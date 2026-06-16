"""The crawl orchestrator: workers pull URLs, fetch, extract, index, expand."""

from __future__ import annotations

import asyncio
import logging
import time

from .config import Config
from .extractors import extract
from .fetcher import Fetcher
from .frontier import Frontier
from .index import Index
from .robots import RobotsCache
from .utils import domain_of, normalize_url, registrable_suffix_match

log = logging.getLogger("crawler")


class WebCrawler:
    def __init__(self, config: Config, index: Index | None = None) -> None:
        self.config = config
        self.index = index or Index(config.db_path)
        self.frontier = Frontier()
        self._stop = asyncio.Event()
        self._pages_done = 0
        self._counter_lock = asyncio.Lock()

        # Per-host serialisation + last-access time for politeness.
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._host_last: dict[str, float] = {}

    # ------------------------------------------------------------------ #
    async def run(self) -> dict:
        seeds = [normalize_url(s) for s in self.config.seeds]
        seeds = [s for s in seeds if s]
        if not seeds:
            raise ValueError("No valid seed URLs configured")

        start = time.time()
        async with Fetcher(self.config) as fetcher:
            self._fetcher = fetcher
            self._robots = RobotsCache(fetcher, self.config.user_agent)

            for s in seeds:
                self.frontier.add(s, 0)

            workers = [
                asyncio.create_task(self._worker(i))
                for i in range(self.config.concurrency)
            ]
            await self.frontier.join()
            self._stop.set()
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

        elapsed = time.time() - start
        log.info("Crawl finished: %d pages in %.1fs", self._pages_done, elapsed)
        return {
            "pages_crawled": self._pages_done,
            "urls_seen": self.frontier.seen_count,
            "elapsed_seconds": round(elapsed, 1),
        }

    # ------------------------------------------------------------------ #
    async def _worker(self, worker_id: int) -> None:
        while True:
            url, depth = await self.frontier.get()
            try:
                if not self._stop.is_set():
                    await self._process(url, depth)
            except Exception:  # never let one bad URL kill a worker
                log.exception("Unhandled error processing %s", url)
            finally:
                self.frontier.task_done()

    async def _process(self, url: str, depth: int) -> None:
        if not self._in_scope(url):
            return
        if self.config.respect_robots and not await self._robots.allowed(url):
            log.debug("Blocked by robots.txt: %s", url)
            return

        await self._politeness_wait(url)
        result = await self._fetcher.fetch(url)
        if not result.ok:
            log.debug("Fetch failed (%s): %s", result.status or result.error, url)
            return

        # Index according to content type.
        final_url = normalize_url(result.url) or url
        doc = extract(final_url, result.content_type, result.body)
        is_textual = result.content_type in self.config.textual_content_types or bool(
            doc.text
        )

        if is_textual or self.config.index_all_types:
            await asyncio.to_thread(
                self.index.upsert,
                final_url,
                doc.title,
                doc.text,
                doc.content_type or result.content_type,
                depth,
                len(result.body),
            )
            async with self._counter_lock:
                self._pages_done += 1
                done = self._pages_done
            if done % 25 == 0:
                log.info("Indexed %d pages (queue=%d)", done, self.frontier.qsize())
            if done >= self.config.max_pages:
                self._stop.set()

        # Expand the frontier with discovered links.
        if depth < self.config.max_depth and not self._stop.is_set():
            for link in doc.links:
                if self._in_scope(link):
                    self.frontier.add(link, depth + 1)

    # ------------------------------------------------------------------ #
    def _in_scope(self, url: str) -> bool:
        host = domain_of(url)
        if not host:
            return False
        if any(registrable_suffix_match(host, b) for b in self.config.blocked_domains):
            return False
        if self.config.allowed_domains:
            return any(
                registrable_suffix_match(host, a)
                for a in self.config.allowed_domains
            )
        if self.config.same_domain_only:
            seed_hosts = {domain_of(s) for s in self.config.seeds}
            return any(registrable_suffix_match(host, h) for h in seed_hosts if h)
        return True

    async def _politeness_wait(self, url: str) -> None:
        host = domain_of(url)
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            delay = self.config.politeness_delay
            robots_delay = (
                await self._robots.crawl_delay(url)
                if self.config.respect_robots
                else None
            )
            if robots_delay:
                delay = max(delay, robots_delay)

            last = self._host_last.get(host)
            if last is not None:
                wait = delay - (time.time() - last)
                if wait > 0:
                    await asyncio.sleep(wait)
            self._host_last[host] = time.time()
