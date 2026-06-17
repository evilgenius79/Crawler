"""Optional headless-browser fetcher for JavaScript-rendered pages.

This sits behind the same ``FetchResult`` contract as :mod:`crawler.fetcher`, so
the crawler can swap it in transparently when ``render_js`` is enabled. Playwright
is a heavy, optional dependency (it needs browser binaries), so importing it is
deferred and failures degrade gracefully to the plain HTTP fetcher.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from urllib.parse import urlparse

from . import security
from .fetcher import FetchResult

log = logging.getLogger("crawler.render")

# Substrings that appear on bot-challenge interstitials (Cloudflare et al.).
CHALLENGE_MARKERS = (
    "just a moment",
    "checking your browser",
    "checking if the site connection is secure",
    "verify you are human",
    "needs to review the security of your connection",
    "cf-challenge",
    "challenge-platform",
    "turnstile",
    "attention required",
)

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


class RealBrowser:
    """A visible, persistent Chromium for crawling sites behind bot challenges.

    Because it is a genuine browser with a saved profile, it presents a real
    fingerprint and keeps cookies (including a Cloudflare clearance) between
    pages and runs. When a site shows a challenge, the window is visible so you
    can solve it once; the crawler waits, then carries on and reuses the
    clearance for the rest of that site.
    """

    def __init__(self, config) -> None:
        self.config = config
        self._pw = None
        self._context = None

    @property
    def available(self) -> bool:
        return self._context is not None

    def _profile_dir(self) -> str:
        return self.config.browser_profile_dir or str(
            Path(self.config.data_dir) / "browser-profile"
        )

    def _user_agent(self) -> str | None:
        # Use the browser's own genuine UA unless the user set a custom one —
        # sending our bot UA would defeat the whole point.
        ua = self.config.user_agent or ""
        return ua if ua and not ua.startswith("PersonalCrawler") else None

    async def __aenter__(self) -> "RealBrowser":
        try:
            from playwright.async_api import async_playwright
        except Exception:
            log.warning(
                "real_browser is on but Playwright is not installed; "
                "falling back to plain HTTP fetching. Install with: "
                "pip install playwright && playwright install chromium"
            )
            return self
        profile = self._profile_dir()
        Path(profile).mkdir(parents=True, exist_ok=True)
        try:
            self._pw = await async_playwright().start()
            self._context = await self._pw.chromium.launch_persistent_context(
                profile,
                headless=False,
                user_agent=self._user_agent(),
                viewport={"width": 1280, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
            )
            if self.config.block_private_addresses:
                await self._context.route("**/*", self._guard_route)
            log.info("Real browser ready (profile: %s).", profile)
        except Exception as exc:
            log.warning(
                "Could not launch a real browser (%s). It needs a desktop "
                "session; falling back to plain HTTP fetching.",
                exc,
            )
            await self._shutdown()
        return self

    async def __aexit__(self, *exc) -> None:
        await self._shutdown()

    async def _shutdown(self) -> None:
        try:
            if self._context:
                await self._context.close()
            if self._pw:
                await self._pw.stop()
        finally:
            self._context = None
            self._pw = None

    async def _guard_route(self, route) -> None:
        host = urlparse(route.request.url).hostname or ""
        safe = host and await asyncio.to_thread(security.is_public_host, host)
        if safe:
            await route.continue_()
        else:
            await route.abort()

    async def fetch(self, url: str) -> FetchResult:
        if not self.available:
            return FetchResult(url=url, status=0, content_type="", body=b"", error="browser unavailable")
        page = await self._context.new_page()
        try:
            resp = await page.goto(
                url, wait_until="domcontentloaded",
                timeout=self.config.request_timeout * 1000,
            )
            if await self._is_challenge(page, resp):
                if not await self._wait_until_cleared(page, url):
                    return FetchResult(
                        url=url, status=0, content_type="", body=b"",
                        error="bot challenge not solved in time",
                    )
                resp = None  # page now holds the real content

            if resp is not None:
                ctype = (resp.headers.get("content-type", "") or "").split(";", 1)[0].strip().lower()
                if ctype and "html" not in ctype and "xml" not in ctype:
                    # Let the plain HTTP fetcher handle non-HTML (PDFs etc.).
                    return FetchResult(url=url, status=resp.status, content_type=ctype, body=b"", error="non-html")

            if self.config.render_wait_ms:
                await page.wait_for_timeout(self.config.render_wait_ms)
            html = await page.content()
            body = html.encode("utf-8", errors="replace")[: self.config.max_content_bytes]
            status = resp.status if resp is not None else 200
            return FetchResult(url=page.url, status=status, content_type="text/html", body=body)
        except Exception as exc:
            return FetchResult(url=url, status=0, content_type="", body=b"", error=repr(exc))
        finally:
            await page.close()

    async def _is_challenge(self, page, resp) -> bool:
        try:
            title = (await page.title() or "").lower()
        except Exception:
            title = ""
        if any(m in title for m in CHALLENGE_MARKERS):
            return True
        status = resp.status if resp is not None else 200
        if status in (403, 503, 429):
            try:
                snippet = (await page.content())[:4000].lower()
            except Exception:
                snippet = ""
            if any(m in snippet for m in CHALLENGE_MARKERS):
                return True
        return False

    async def _wait_until_cleared(self, page, url: str) -> bool:
        timeout = self.config.browser_solve_timeout
        log.warning(
            "Bot challenge on %s — solve it in the browser window "
            "(waiting up to %ds)...", url, timeout,
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            await asyncio.sleep(2)
            if not await self._is_challenge(page, None):
                log.info("Challenge cleared for %s", url)
                return True
        log.warning("Challenge not solved in time for %s", url)
        return False
