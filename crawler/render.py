"""Optional headless-browser fetcher for JavaScript-rendered pages.

This sits behind the same ``FetchResult`` contract as :mod:`crawler.fetcher`, so
the crawler can swap it in transparently when ``render_js`` is enabled. Playwright
is a heavy, optional dependency (it needs browser binaries), so importing it is
deferred and failures degrade gracefully to the plain HTTP fetcher.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlparse

from . import security
from .fetcher import FetchResult

log = logging.getLogger("crawler.render")


class JSRenderer:
    """Render pages with a shared headless Chromium instance."""

    def __init__(self, config) -> None:
        self.config = config
        self._playwright = None
        self._browser = None

    @property
    def available(self) -> bool:
        return self._browser is not None

    async def __aenter__(self) -> "JSRenderer":
        try:
            from playwright.async_api import async_playwright
        except Exception:
            log.warning(
                "render_js is on but Playwright is not installed; "
                "falling back to plain HTTP fetching. "
                "Install with: pip install playwright && playwright install chromium"
            )
            return self
        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(headless=True)
        except Exception as exc:  # browsers not installed, sandbox limits, etc.
            log.warning("Could not launch headless browser (%s); using HTTP fetch", exc)
            await self._shutdown()
        return self

    async def __aexit__(self, *exc) -> None:
        await self._shutdown()

    async def _shutdown(self) -> None:
        try:
            if self._browser:
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
        finally:
            self._browser = None
            self._playwright = None

    async def fetch(self, url: str) -> FetchResult:
        """Load ``url`` in a browser tab and return the rendered HTML."""
        if not self.available:
            return FetchResult(url=url, status=0, content_type="", body=b"", error="renderer unavailable")
        context = None
        try:
            context = await self._browser.new_context(user_agent=self.config.user_agent)
            page = await context.new_page()
            # The browser does its own networking and is NOT covered by the
            # fetcher's connect-time SSRF resolver, so block requests (main page,
            # redirects and sub-resources) to private addresses here. Best-effort:
            # this re-resolves at request time but cannot fully close a rebinding
            # race inside the browser's own stack.
            if self.config.block_private_addresses:
                await page.route("**/*", self._guard_route)
            resp = await page.goto(
                url, wait_until="networkidle", timeout=self.config.request_timeout * 1000
            )
            if self.config.render_wait_ms:
                await page.wait_for_timeout(self.config.render_wait_ms)
            html = await page.content()
            status = resp.status if resp else 200
            body = html.encode("utf-8", errors="replace")[: self.config.max_content_bytes]
            return FetchResult(
                url=page.url,
                status=status,
                content_type="text/html",
                body=body,
            )
        except Exception as exc:
            return FetchResult(url=url, status=0, content_type="", body=b"", error=repr(exc))
        finally:
            if context:
                await context.close()

    async def _guard_route(self, route) -> None:
        host = urlparse(route.request.url).hostname or ""
        safe = host and await asyncio.to_thread(security.is_public_host, host)
        if safe:
            await route.continue_()
        else:
            log.debug("Renderer blocked request to private address: %s", route.request.url)
            await route.abort()
