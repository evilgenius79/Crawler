"""Small URL helpers used across the crawler."""

from __future__ import annotations

import hashlib
from urllib.parse import urldefrag, urljoin, urlparse, urlunparse

# Query parameters that only track users and never change page content.
_TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gclid",
    "fbclid",
    "mc_cid",
    "mc_eid",
}


def normalize_url(url: str, base: str | None = None) -> str | None:
    """Resolve, clean and canonicalise a URL.

    Returns ``None`` for anything that is not an http(s) URL so callers can
    cheaply skip mailto:, javascript:, tel:, data: and friends.
    """
    if not url:
        return None
    url = url.strip()
    if base:
        url = urljoin(base, url)
    url, _ = urldefrag(url)

    parts = urlparse(url)
    if parts.scheme not in ("http", "https"):
        return None
    if not parts.netloc:
        return None

    netloc = parts.netloc.lower()
    # Drop redundant default ports.
    if parts.scheme == "http" and netloc.endswith(":80"):
        netloc = netloc[:-3]
    elif parts.scheme == "https" and netloc.endswith(":443"):
        netloc = netloc[:-4]

    # Strip well-known tracking params while preserving meaningful ones.
    query = parts.query
    if query:
        kept = [
            pair
            for pair in query.split("&")
            if pair.split("=", 1)[0] not in _TRACKING_PARAMS
        ]
        query = "&".join(kept)

    path = parts.path or "/"
    return urlunparse((parts.scheme, netloc, path, parts.params, query, ""))


def url_hash(url: str) -> str:
    """Stable short id for a URL (used for de-duplication)."""
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


def domain_of(url: str) -> str:
    """Return the lowercased host of a URL (without port)."""
    netloc = urlparse(url).netloc.lower()
    return netloc.split(":", 1)[0]


def registrable_suffix_match(host: str, domain: str) -> bool:
    """True if ``host`` equals ``domain`` or is a subdomain of it."""
    host = host.lower()
    domain = domain.lower()
    return host == domain or host.endswith("." + domain)
