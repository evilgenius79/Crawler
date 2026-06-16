"""The URL frontier: a de-duplicated BFS queue of (url, depth)."""

from __future__ import annotations

import asyncio

from .utils import url_hash


class Frontier:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[tuple[str, int]] = asyncio.Queue()
        self._seen: set[str] = set()

    def add(self, url: str, depth: int) -> bool:
        """Enqueue a URL if we have not seen it. Returns True if added."""
        key = url_hash(url)
        if key in self._seen:
            return False
        self._seen.add(key)
        self._queue.put_nowait((url, depth))
        return True

    async def get(self) -> tuple[str, int]:
        return await self._queue.get()

    def task_done(self) -> None:
        self._queue.task_done()

    async def join(self) -> None:
        await self._queue.join()

    def qsize(self) -> int:
        return self._queue.qsize()

    @property
    def seen_count(self) -> int:
        return len(self._seen)
