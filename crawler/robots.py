"""Per-host robots.txt fetching and caching."""

from __future__ import annotations

import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser


class RobotsCache:
    """Caches one RobotFileParser per host, refreshed after ``ttl`` seconds."""

    def __init__(self, fetcher, user_agent: str, ttl: int = 3600) -> None:
        self._fetcher = fetcher
        self._user_agent = user_agent
        self._ttl = ttl
        self._cache: dict[str, tuple[float, RobotFileParser]] = {}

    async def _parser_for(self, url: str) -> RobotFileParser:
        parts = urlparse(url)
        host_key = f"{parts.scheme}://{parts.netloc}"
        cached = self._cache.get(host_key)
        if cached and (time.time() - cached[0]) < self._ttl:
            return cached[1]

        parser = RobotFileParser()
        robots_url = f"{host_key}/robots.txt"
        result = await self._fetcher.fetch(robots_url)
        if result.ok and result.body:
            text = result.body.decode("utf-8", errors="replace")
            parser.parse(text.splitlines())
        else:
            # No robots.txt (or it errored) => allow everything.
            parser.parse([])
        self._cache[host_key] = (time.time(), parser)
        return parser

    async def allowed(self, url: str) -> bool:
        parser = await self._parser_for(url)
        return parser.can_fetch(self._user_agent, url)

    async def crawl_delay(self, url: str) -> float | None:
        parser = await self._parser_for(url)
        try:
            delay = parser.crawl_delay(self._user_agent)
        except Exception:
            return None
        return float(delay) if delay is not None else None
