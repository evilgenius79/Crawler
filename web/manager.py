"""In-process crawl manager: lets the web admin start/stop/extend crawls.

A single crawl runs as a background task on the web server's event loop. The
manager exposes start/stop/add/status so the admin dashboard can drive it.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import time

from crawler.config import Config
from crawler.crawler import WebCrawler

log = logging.getLogger("crawler.manager")


class CrawlManager:
    def __init__(self, base_config: Config) -> None:
        self._base = base_config
        self._crawler: WebCrawler | None = None
        self._task: asyncio.Task | None = None
        self._started_at: float | None = None
        self._last_summary: dict | None = None
        self._last_error: str | None = None
        self._last_seeds: list[str] = []

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def _build_config(self, seeds: list[str], overrides: dict) -> Config:
        # Deep copy so per-crawl tweaks never alias the base config's list fields
        # (allowed_domains, blocked_domains, …) shared with the search endpoints.
        cfg = copy.deepcopy(self._base)
        cfg.seeds = list(seeds)
        for key in (
            "max_pages",
            "max_depth",
            "concurrency",
            "same_domain_only",
            "respect_robots",
            "render_js",
            "politeness_delay",
        ):
            if overrides.get(key) is not None:
                setattr(cfg, key, overrides[key])
        return cfg

    async def start(self, seeds: list[str], overrides: dict | None = None) -> None:
        if self.running:
            raise RuntimeError("A crawl is already running")
        seeds = [s.strip() for s in seeds if s and s.strip()]
        if not seeds:
            raise ValueError("Provide at least one URL to crawl")
        cfg = self._build_config(seeds, overrides or {})
        self._crawler = WebCrawler(cfg)
        self._started_at = time.time()
        self._last_seeds = seeds
        self._last_error = None
        self._last_summary = None
        self._task = asyncio.create_task(self._run())
        log.info("Crawl started from admin with %d seed(s)", len(seeds))

    async def _run(self) -> None:
        assert self._crawler is not None
        try:
            self._last_summary = await self._crawler.run()
        except Exception as exc:  # surface failures in the dashboard
            self._last_error = repr(exc)
            log.exception("Crawl task failed")

    def stop(self) -> bool:
        """Signal the running crawl to wind down. Returns False if none running."""
        if self._crawler and self.running:
            self._crawler.stop()
            log.info("Crawl stop requested from admin")
            return True
        return False

    async def add_urls(self, urls: list[str], overrides: dict | None = None) -> dict:
        """Add URLs to the running crawl, or start a new one if idle."""
        urls = [u.strip() for u in urls if u and u.strip()]
        if not urls:
            return {"added": 0, "started": False}
        if self.running and self._crawler:
            added = await self._crawler.add_urls(urls)
            return {"added": added, "started": False}
        await self.start(urls, overrides)
        return {"added": len(urls), "started": True}

    def status(self) -> dict:
        st = {
            "running": self.running,
            "seeds": self._last_seeds,
            "last_summary": self._last_summary,
            "last_error": self._last_error,
            "started_at": self._started_at,
        }
        if self._crawler is not None:
            st.update(self._crawler.status())
        return st
