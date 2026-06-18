"""Discover URLs from robots.txt and sitemap.xml for fuller crawl coverage.

Link-following only finds pages reachable from the seeds; a sitemap often lists
*every* page a site wants indexed. This is best-effort: any failure just yields
fewer URLs and the crawl falls back to ordinary link discovery.
"""

from __future__ import annotations

import gzip
import logging
import re
from urllib.parse import urlparse

from .utils import normalize_url

log = logging.getLogger("crawler.sitemap")

_LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.IGNORECASE | re.DOTALL)
_SITEMAP_DIRECTIVE_RE = re.compile(r"^\s*Sitemap:\s*(\S+)", re.IGNORECASE | re.MULTILINE)


def _decode(body: bytes, url: str) -> str:
    if url.endswith(".gz") or body[:2] == b"\x1f\x8b":
        try:
            body = gzip.decompress(body)
        except Exception:
            pass
    return body.decode("utf-8", "replace")


async def discover(fetcher, seed_url: str, max_urls: int = 5000, max_sitemaps: int = 25) -> list[str]:
    """Return the page URLs listed in a site's sitemap(s)."""
    parts = urlparse(seed_url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return []
    base = f"{parts.scheme}://{parts.netloc}"

    queue: list[str] = []
    seen_sitemaps: set[str] = set()

    def _enqueue(u: str) -> None:
        n = normalize_url(u)
        if n and n not in seen_sitemaps:
            seen_sitemaps.add(n)
            queue.append(n)

    # robots.txt may point at one or more sitemaps; also try the usual defaults.
    robots = await fetcher.fetch(base + "/robots.txt")
    if robots.ok and robots.body:
        for sm in _SITEMAP_DIRECTIVE_RE.findall(robots.body.decode("utf-8", "replace")):
            _enqueue(sm)
    _enqueue(base + "/sitemap.xml")
    _enqueue(base + "/sitemap_index.xml")

    found: list[str] = []
    found_set: set[str] = set()
    processed = 0
    while queue and processed < max_sitemaps and len(found) < max_urls:
        sm = queue.pop(0)
        processed += 1
        res = await fetcher.fetch(sm)
        if not res.ok or not res.body:
            continue
        body = _decode(res.body, sm)
        is_index = "<sitemapindex" in body[:2000].lower()
        for loc in _LOC_RE.findall(body):
            n = normalize_url(loc.strip())
            if not n:
                continue
            if is_index:
                _enqueue(n)  # nested sitemap -> crawl it too
            elif n not in found_set:
                found_set.add(n)
                found.append(n)
                if len(found) >= max_urls:
                    break

    if found:
        log.info("Sitemap discovery found %d URLs for %s", len(found), base)
    return found
