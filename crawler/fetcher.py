"""Async HTTP fetching with timeouts and a hard size cap."""

from __future__ import annotations

from dataclasses import dataclass

import aiohttp
from aiohttp.resolver import DefaultResolver

from .security import _is_blocked_ip


class SafeResolver(aiohttp.abc.AbstractResolver):
    """A resolver that refuses to hand back private/loopback/reserved IPs.

    Filtering at resolution (connect) time is what actually closes SSRF holes:
    it covers redirects and DNS-rebinding, which a pre-request hostname check
    cannot, because the connection only ever happens to a vetted address.
    """

    def __init__(self) -> None:
        self._inner = DefaultResolver()

    async def resolve(self, host, port=0, family=0):
        infos = await self._inner.resolve(host, port, family)
        safe = [i for i in infos if not _is_blocked_ip(i["host"])]
        if not safe:
            raise OSError(f"refusing to connect to private address for host {host!r}")
        return safe

    async def close(self) -> None:
        await self._inner.close()


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
        connector = None
        if self.config.block_private_addresses:
            # Block private addresses at connect time (covers redirects/rebinding).
            connector = aiohttp.TCPConnector(resolver=SafeResolver())
        self._session = aiohttp.ClientSession(
            timeout=timeout,
            headers={"User-Agent": self.config.user_agent},
            connector=connector,
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
