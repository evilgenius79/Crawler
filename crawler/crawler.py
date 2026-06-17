"""The crawl orchestrator: workers pull URLs, fetch, extract, index, expand."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from contextlib import AsyncExitStack

from . import security
from .config import Config
from .extractors import extract
from .fetcher import Fetcher
from .frontier import Frontier
from .index import Index
from .render import JSRenderer, RealBrowser
from .robots import RobotsCache
from .utils import domain_of, normalize_url, registrable_suffix_match

log = logging.getLogger("crawler")


class WebCrawler:
    def __init__(self, config: Config, index: Index | None = None) -> None:
        self.config = config
        self._owns_index = index is None
        self.index = index or Index(config.db_path)
        self.frontier = Frontier()
        self._stop = asyncio.Event()
        self._pages_done = 0
        self._errors = 0
        self._duplicates = 0
        # A rolling window of recent failures (url + reason) for the dashboard.
        self._recent_errors: deque[dict] = deque(maxlen=50)
        self._counter_lock = asyncio.Lock()

        # Per-host serialisation + last-access time for politeness.
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._host_last: dict[str, float] = {}
        # Cache of host -> is-public so we resolve each host at most once.
        self._host_safe: dict[str, bool] = {}

        self._renderer: JSRenderer | None = None
        self._browser: RealBrowser | None = None
        self._start_time: float | None = None
        self._stopped_by_user = False
        # Set by the web manager so errors can be persisted against the run.
        self.run_id: int | None = None

    # ------------------------------------------------------------------ #
    # Live control surface (used by the web admin dashboard)
    # ------------------------------------------------------------------ #
    def stop(self) -> None:
        """Signal the crawl to wind down; in-flight workers drain the queue."""
        self._stopped_by_user = True
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    @property
    def stopped_by_user(self) -> bool:
        return self._stopped_by_user

    def status(self) -> dict:
        elapsed = (time.time() - self._start_time) if self._start_time else 0.0
        return {
            "pages_indexed": self._pages_done,
            "errors": self._errors,
            "duplicates": self._duplicates,
            "queued": self.frontier.qsize(),
            "seen": self.frontier.seen_count,
            "elapsed_seconds": round(elapsed, 1),
            "stopping": self._stop.is_set(),
            # Most recent failures first.
            "recent_errors": list(self._recent_errors)[-15:][::-1],
        }

    async def _note_error(self, url: str, reason: str) -> None:
        """Record a failure for the live panel and (for web crawls) persist it."""
        self._recent_errors.append({"url": url, "reason": reason, "when": time.time()})
        if self.run_id is not None:
            await asyncio.to_thread(self.index.add_crawl_error, self.run_id, url, reason)

    async def add_urls(self, urls: list[str], depth: int = 0) -> int:
        """Inject URLs into a running (or about-to-run) crawl's frontier."""
        new: list[tuple[str, int]] = []
        for u in urls:
            n = normalize_url(u)
            if n and self.frontier.add(n, depth):
                new.append((n, depth))
        if new and self.config.resume:
            await asyncio.to_thread(self.index.frontier_add_many, new)
        return len(new)

    # ------------------------------------------------------------------ #
    async def run(self) -> dict:
        try:
            return await self._run()
        finally:
            if self._owns_index:
                self.index.close()

    async def _run(self) -> dict:
        seeds = [normalize_url(s) for s in self.config.seeds]
        seeds = [s for s in seeds if s]

        self._restore_frontier(seeds)
        if self.frontier.qsize() == 0:
            log.info("Nothing to crawl (no seeds, pending, or stale URLs).")
            return {"pages_crawled": 0, "urls_seen": 0, "elapsed_seconds": 0.0}

        start = time.time()
        self._start_time = start
        # A real browser does one page at a time (and one challenge window).
        concurrency = 1 if self.config.real_browser else max(1, self.config.concurrency)
        log.info(
            "Starting crawl: %d URLs queued, concurrency=%d, robots=%s%s",
            self.frontier.qsize(),
            concurrency,
            "on" if self.config.respect_robots else "off",
            ", real-browser" if self.config.real_browser else "",
        )
        async with AsyncExitStack() as stack:
            fetcher = await stack.enter_async_context(Fetcher(self.config))
            self._fetcher = fetcher
            self._robots = RobotsCache(fetcher, self.config.user_agent)
            if self.config.real_browser:
                self._browser = await stack.enter_async_context(RealBrowser(self.config))
            elif self.config.render_js:
                self._renderer = await stack.enter_async_context(JSRenderer(self.config))

            workers = [
                asyncio.create_task(self._worker(i))
                for i in range(concurrency)
            ]
            reporter = asyncio.create_task(self._progress_reporter())
            await self.frontier.join()
            self._stop.set()
            for w in workers:
                w.cancel()
            reporter.cancel()
            await asyncio.gather(*workers, reporter, return_exceptions=True)

        elapsed = time.time() - start
        log.info(
            "Crawl finished: %d pages indexed, %d errors, %.1fs",
            self._pages_done,
            self._errors,
            elapsed,
        )
        return {
            "pages_crawled": self._pages_done,
            "urls_seen": self.frontier.seen_count,
            "errors": self._errors,
            "elapsed_seconds": round(elapsed, 1),
        }

    async def _progress_reporter(self) -> None:
        """Print a heartbeat every few seconds so you can see the crawl is alive."""
        last_done = 0
        last_t = self._start_time
        try:
            while True:
                await asyncio.sleep(self.config.progress_interval)
                now = time.time()
                done = self._pages_done
                rate = (done - last_done) / (now - last_t) if now > last_t else 0.0
                log.info(
                    "progress: %d indexed | %d queued | %d errors | %.1f pages/s",
                    done,
                    self.frontier.qsize(),
                    self._errors,
                    rate,
                )
                last_done, last_t = done, now
        except asyncio.CancelledError:
            pass

    def _restore_frontier(self, seeds: list[str]) -> None:
        """Seed the frontier, resuming persisted state and re-queuing stale docs."""
        # Re-crawling is inherently a persistent-frontier operation: it re-queues
        # rows and relies on done-marking. Honour it even if the user passed
        # --no-resume, otherwise the re-crawl would silently do nothing.
        if self.config.recrawl_after_days > 0:
            self.config.resume = True

        if self.config.resume:
            if self.config.recrawl_after_days > 0:
                n = self.index.requeue_stale(self.config.recrawl_after_days * 86400)
                if n:
                    log.info("Re-queued %d documents for re-crawl", n)
            pending = self.index.frontier_pending()
            known = self.index.frontier_known_urls()
            self.frontier.load(pending, known)
            if pending:
                log.info("Resuming with %d pending URLs from a previous crawl", len(pending))

        fresh = [s for s in seeds if self.frontier.add(s, 0)]
        if self.config.resume and fresh:
            self.index.frontier_add_many([(s, 0) for s in fresh])

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
        if not await self._host_is_safe(url):
            log.debug("Blocked unsafe/private address: %s", url)
            await self._note_error(url, "blocked: unresolved or private/unsafe host")
            await self._persist_state(url, "error")
            return
        if self.config.respect_robots and not await self._robots.allowed(url):
            log.debug("Blocked by robots.txt: %s", url)
            await self._persist_state(url, "done")
            return

        await self._politeness_wait(url)
        result = await self._fetch(url)
        if not result.ok:
            reason = f"HTTP {result.status}" if result.status else (result.error or "fetch failed")
            log.debug("Fetch failed (%s): %s", reason, url)
            self._errors += 1
            await self._note_error(url, reason)
            await self._persist_state(url, "error")
            return

        # Redirects to private hosts are already refused at connect time by the
        # fetcher's SafeResolver, so a successful result here is from a vetted
        # address; no second host check is needed.
        final_url = normalize_url(result.url) or url
        doc = extract(final_url, result.content_type, result.body)
        is_textual = result.content_type in self.config.textual_content_types or bool(
            doc.text
        )

        if is_textual or self.config.index_all_types:
            stored = await asyncio.to_thread(
                self.index.upsert,
                final_url,
                doc.title,
                doc.text,
                doc.content_type or result.content_type,
                depth,
                len(result.body),
                self.config.deduplicate,
            )
            if stored:
                async with self._counter_lock:
                    self._pages_done += 1
                    done = self._pages_done
                # Per-second progress is emitted by _progress_reporter.
                if done >= self.config.max_pages:
                    self._stop.set()
            else:
                self._duplicates += 1

        await self._persist_state(url, "done")

        # Expand the frontier with discovered links.
        if depth < self.config.max_depth and not self._stop.is_set():
            new_links = [
                link
                for link in doc.links
                if self._in_scope(link) and self.frontier.add(link, depth + 1)
            ]
            if self.config.resume and new_links:
                await asyncio.to_thread(
                    self.index.frontier_add_many, [(u, depth + 1) for u in new_links]
                )

    async def _fetch(self, url: str):
        """Fetch a URL, via the real browser, headless renderer, or plain HTTP."""
        # Real-browser mode: the browser carries cookies/challenge clearance.
        if self._browser and self._browser.available:
            result = await self._browser.fetch(url)
            if result.error == "non-html":
                return await self._fetcher.fetch(url)  # PDFs etc. over plain HTTP
            return result

        result = await self._fetcher.fetch(url)
        if (
            self._renderer
            and self._renderer.available
            and result.ok
            and result.content_type in ("text/html", "application/xhtml+xml")
        ):
            rendered = await self._renderer.fetch(url)
            if rendered.ok:
                return rendered
        return result

    async def _persist_state(self, url: str, state: str) -> None:
        if self.config.resume:
            await asyncio.to_thread(self.index.frontier_mark, url, state)

    # ------------------------------------------------------------------ #
    def _in_scope(self, url: str) -> bool:
        host = domain_of(url)
        if not host:
            return False
        if any(pat and pat in url for pat in self.config.exclude_patterns):
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

    async def _host_is_safe(self, url: str) -> bool:
        if not self.config.block_private_addresses:
            return True
        host = domain_of(url)
        cached = self._host_safe.get(host)
        if cached is not None:
            return cached
        # DNS resolution blocks, so run it off the event loop.
        safe = await asyncio.to_thread(security.url_is_safe, url)
        self._host_safe[host] = safe
        return safe

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
