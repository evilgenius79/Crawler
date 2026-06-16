"""Async HTTP fetching with timeouts and a hard size cap."""

from __future__ import annotations

from dataclasses import dataclass

import aiohttp


@dataclass
class FetchResult:
    url: str  # final URL after redirects
    status: int
    content_type: str
    body: bytes
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 300


class Fetcher:
    """Thin wrapper around a shared aiohttp session."""

    def __init__(self, config) -> None:
        self.config = config
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "Fetcher":
        timeout = aiohttp.ClientTimeout(total=self.config.request_timeout)
        self._session = aiohttp.ClientSession(
            timeout=timeout,
            headers={"User-Agent": self.config.user_agent},
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def fetch(self, url: str) -> FetchResult:
        assert self._session is not None, "Fetcher must be used as a context manager"
        try:
            async with self._session.get(url, allow_redirects=True) as resp:
                ctype = resp.headers.get("Content-Type", "").split(";", 1)[0].strip()
                body = await self._read_capped(resp)
                return FetchResult(
                    url=str(resp.url),
                    status=resp.status,
                    content_type=ctype.lower(),
                    body=body,
                )
        except aiohttp.ClientError as exc:
            return FetchResult(url=url, status=0, content_type="", body=b"", error=str(exc))
        except Exception as exc:  # network/DNS/timeout/etc.
            return FetchResult(
                url=url, status=0, content_type="", body=b"", error=repr(exc)
            )

    async def _read_capped(self, resp: aiohttp.ClientResponse) -> bytes:
        """Read at most ``max_content_bytes`` so a huge file can't blow up RAM."""
        cap = self.config.max_content_bytes
        chunks: list[bytes] = []
        total = 0
        async for chunk in resp.content.iter_chunked(64 * 1024):
            chunks.append(chunk)
            total += len(chunk)
            if total >= cap:
                break
        return b"".join(chunks)[:cap]
