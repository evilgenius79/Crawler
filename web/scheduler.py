"""Built-in scheduler: periodically re-crawls to keep the index fresh.

Settings live in the index DB (so they survive restarts) and are edited from the
admin dashboard. When enabled, a background task wakes every `interval_hours`
and, if no crawl is already running, kicks off a re-crawl.
"""

from __future__ import annotations

import asyncio
import logging

from crawler.index import Index

from .manager import CrawlManager

log = logging.getLogger("crawler.scheduler")

DEFAULTS = {"enabled": False, "interval_hours": 24.0, "older_than_days": 1.0, "seeds": []}


class Scheduler:
    def __init__(self, manager: CrawlManager, index: Index) -> None:
        self._manager = manager
        self._index = index
        self._task: asyncio.Task | None = None

    def settings(self) -> dict:
        saved = self._index.get_setting("schedule", {}) or {}
        return {**DEFAULTS, **saved}

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> dict:
        return {**self.settings(), "active": self.active}

    def start(self) -> None:
        """Start the loop if scheduling is enabled (called on server startup)."""
        self._restart()

    async def apply(self, changes: dict) -> dict:
        s = self.settings()
        if "enabled" in changes:
            s["enabled"] = bool(changes["enabled"])
        if changes.get("interval_hours") is not None:
            s["interval_hours"] = max(0.05, float(changes["interval_hours"]))
        if changes.get("older_than_days") is not None:
            s["older_than_days"] = max(0.0, float(changes["older_than_days"]))
        if "seeds" in changes:
            s["seeds"] = changes["seeds"] or []
        await asyncio.to_thread(self._index.set_setting, "schedule", s)
        self._restart()
        return self.status()

    def _restart(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        if self.settings().get("enabled"):
            self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        try:
            while True:
                s = self.settings()
                if not s.get("enabled"):
                    return
                # Clamp defensively so a stray 0 can't busy-spin the loop.
                interval = max(0.05, float(s.get("interval_hours") or 24))
                await asyncio.sleep(interval * 3600)
                s = self.settings()
                if not s.get("enabled"):
                    return
                if self._manager.running:
                    log.info("Scheduled re-crawl skipped: a crawl is already running")
                    continue
                try:
                    log.info("Scheduled re-crawl starting")
                    await self._manager.start_recrawl(
                        float(s["older_than_days"]), s.get("seeds", [])
                    )
                except Exception:
                    log.exception("Scheduled re-crawl failed to start")
        except asyncio.CancelledError:
            pass
