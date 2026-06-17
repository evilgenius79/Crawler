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
from crawler.crawler import WebCrawler, probe_url
from crawler.index import Index

log = logging.getLogger("crawler.manager")


class CrawlManager:
    def __init__(self, base_config: Config, index: Index) -> None:
        self._base = base_config
        self._index = index
        self._crawler: WebCrawler | None = None
        self._task: asyncio.Task | None = None
        self._run_id: int | None = None
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
            "real_browser",
            "politeness_delay",
            "recrawl_after_days",
            "deduplicate",
            "exclude_patterns",
            "user_agent",
        ):
            if overrides.get(key) is not None:
                setattr(cfg, key, overrides[key])
        return cfg

    async def start(self, seeds: list[str], overrides: dict | None = None) -> None:
        if self.running:
            raise RuntimeError("A crawl is already running")
        overrides = overrides or {}
        seeds = [s.strip() for s in seeds if s and s.strip()]
        recrawl = overrides.get("recrawl_after_days") or 0
        if not seeds and recrawl <= 0:
            raise ValueError("Provide at least one URL to crawl")
        cfg = self._build_config(seeds, overrides)
        self._crawler = WebCrawler(cfg)
        self._started_at = time.time()
        self._last_seeds = seeds
        self._last_error = None
        self._last_summary = None
        self._run_id = await asyncio.to_thread(
            self._index.record_crawl_start, seeds, cfg.max_pages, cfg.max_depth
        )
        self._crawler.run_id = self._run_id
        self._task = asyncio.create_task(self._run())
        log.info("Crawl started from admin with %d seed(s)", len(seeds))

    async def test_url(self, url: str, overrides: dict | None = None) -> dict:
        """Fetch one URL (no indexing) to confirm it's reachable before a crawl."""
        overrides = overrides or {}
        if self.running and overrides.get("real_browser"):
            raise RuntimeError("Stop the running crawl before testing with the real browser")
        cfg = self._build_config([url], overrides)
        return await probe_url(cfg, url)

    async def start_recrawl(self, older_than_days: float, seeds: list[str] | None = None) -> None:
        """Re-crawl documents older than N days (used by the scheduler).

        ``older_than_days <= 0`` means "refresh everything"; we use a tiny
        positive window so the >0 re-crawl machinery still engages.
        """
        window = older_than_days if older_than_days > 0 else 1e-9
        await self.start(seeds or [], {"recrawl_after_days": window})

    async def _run(self) -> None:
        assert self._crawler is not None
        status, detail = "finished", None
        try:
            self._last_summary = await self._crawler.run()
            if self._crawler.stopped_by_user:
                status = "stopped"
        except Exception as exc:  # surface failures in the dashboard
            self._last_error = repr(exc)
            status, detail = "error", repr(exc)
            log.exception("Crawl task failed")
        finally:
            if self._run_id is not None:
                st = self._crawler.status()
                await asyncio.to_thread(
                    self._index.record_crawl_finish,
                    self._run_id,
                    status,
                    st["pages_indexed"],
                    st["errors"],
                    detail,
                )

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

    def history(self, limit: int = 12) -> list[dict]:
        rows = self._index.recent_crawls(limit)
        # Show live counts for the row that is currently running.
        if self.running and self._crawler and rows and rows[0].get("id") == self._run_id:
            st = self._crawler.status()
            rows[0]["pages_indexed"] = st["pages_indexed"]
            rows[0]["errors"] = st["errors"]
        return rows

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
