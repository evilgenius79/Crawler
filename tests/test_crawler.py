"""Unit tests that need no network access."""

from __future__ import annotations

import os
import tempfile

from crawler import security
from crawler.extractors import extract
from crawler.frontier import Frontier
from crawler.index import HL_CLOSE, HL_OPEN, Index, _to_fts_query
from crawler.utils import (
    domain_of,
    normalize_url,
    registrable_suffix_match,
    url_hash,
)


def test_normalize_url_resolves_and_strips():
    base = "https://example.com/dir/page.html"
    assert normalize_url("../other?utm_source=x#frag", base) == (
        "https://example.com/other"
    )
    assert normalize_url("HTTP://Example.com:80/") == "http://example.com/"
    assert normalize_url("mailto:a@b.com") is None
    assert normalize_url("javascript:void(0)") is None


def test_url_hash_is_stable():
    assert url_hash("https://a.com") == url_hash("https://a.com")
    assert url_hash("https://a.com") != url_hash("https://b.com")


def test_domain_and_suffix_match():
    assert domain_of("https://Sub.Example.com:443/x") == "sub.example.com"
    assert registrable_suffix_match("docs.example.com", "example.com")
    assert not registrable_suffix_match("notexample.com", "example.com")


def test_extract_html_text_and_links():
    html = b"""
    <html><head><title>Hello World</title></head>
    <body><h1>Hi</h1><p>Some content here.</p>
    <a href="/next">next</a><a href="https://other.com/x">x</a>
    <script>ignore()</script></body></html>
    """
    doc = extract("https://example.com/", "text/html", html)
    assert doc.title == "Hello World"
    assert "Some content here." in doc.text
    assert "ignore" not in doc.text
    assert "https://example.com/next" in doc.links
    assert "https://other.com/x" in doc.links


def test_fts_query_sanitisation():
    assert _to_fts_query('hello "world"') == '"hello" AND "world"'
    assert _to_fts_query("   ") == ""


def test_index_upsert_and_search():
    with tempfile.TemporaryDirectory() as d:
        idx = Index(os.path.join(d, "t.db"))
        idx.upsert(
            "https://example.com/a",
            "Python Tutorial",
            "Learn Python programming with examples.",
            "text/html",
            0,
            123,
        )
        # Upsert same URL again -> still one document.
        idx.upsert(
            "https://example.com/a",
            "Python Tutorial v2",
            "Learn Python programming fast.",
            "text/html",
            0,
            130,
        )
        hits = idx.search("python")
        assert len(hits) == 1
        assert hits[0].url == "https://example.com/a"
        assert hits[0].title == "Python Tutorial v2"
        assert idx.stats()["total_documents"] == 1
        assert idx.count_matches("python") == 1
        assert idx.search("nonexistentterm") == []
        idx.close()


def test_search_snippet_uses_sentinel_markers():
    with tempfile.TemporaryDirectory() as d:
        idx = Index(os.path.join(d, "t.db"))
        idx.upsert(
            "https://x/a", "T", "the quick brown fox jumps", "text/html", 0, 1
        )
        hit = idx.search("quick")[0]
        # Highlights use private-use sentinels, never raw HTML markup.
        assert HL_OPEN in hit.snippet and HL_CLOSE in hit.snippet
        assert "<mark>" not in hit.snippet
        idx.close()


def test_safe_snippet_escapes_page_html(monkeypatch):
    # Point the web module at a throwaway DB before importing it.
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("CRAWLER_DATA_DIR", d)
        import importlib

        import web.app as app

        importlib.reload(app)
        raw = f"safe {HL_OPEN}<script>alert(1)</script>{HL_CLOSE} text"
        out = str(app.safe_snippet(raw))
        assert "<script>" not in out  # page markup is neutralised
        assert "&lt;script&gt;" in out
        assert "<mark>&lt;script&gt;alert(1)&lt;/script&gt;</mark>" in out
        # The JSON API helper must escape page text too (no raw <script>).
        api_snip = app._escaped_snippet_html(raw)
        assert "<script>" not in api_snip
        assert "&lt;script&gt;" in api_snip
        assert "<mark>" in api_snip


def test_blocked_ip_ranges():
    assert security._is_blocked_ip("127.0.0.1")
    assert security._is_blocked_ip("10.1.2.3")
    assert security._is_blocked_ip("192.168.0.5")
    assert security._is_blocked_ip("169.254.169.254")  # cloud metadata
    assert security._is_blocked_ip("::1")
    assert security._is_blocked_ip("not-an-ip")
    assert not security._is_blocked_ip("8.8.8.8")


def test_is_public_host_with_literals():
    # IP literals resolve offline via getaddrinfo, so this needs no network.
    assert security.is_public_host("8.8.8.8")
    assert not security.is_public_host("127.0.0.1")
    assert not security.is_public_host("10.0.0.1")
    assert not security.is_public_host("")


def test_frontier_persistence_and_recrawl():
    with tempfile.TemporaryDirectory() as d:
        idx = Index(os.path.join(d, "t.db"))
        idx.frontier_add_many([("https://x/a", 0), ("https://x/b", 1)])
        idx.frontier_add_many([("https://x/a", 0)])  # dup ignored
        assert idx.frontier_known_urls() == {"https://x/a", "https://x/b"}
        assert len(idx.frontier_pending()) == 2

        idx.frontier_mark("https://x/a", "done")
        assert len(idx.frontier_pending()) == 1

        # A document old enough to be re-queued.
        idx.upsert("https://x/a", "A", "content", "text/html", 0, 1)
        idx._write("UPDATE docs SET crawled_at = 0 WHERE url = ?", ("https://x/a",))
        assert idx.requeue_stale(older_than_seconds=10) == 1
        assert ("https://x/a", 0) in idx.frontier_pending()
        idx.close()


def test_frontier_resume_load():
    f = Frontier()
    f.load(pending=[("https://x/a", 2)], known={"https://x/a", "https://x/b"})
    # Known URLs are not re-added.
    assert f.add("https://x/b", 0) is False
    assert f.add("https://x/c", 0) is True
    # Pending URL is queued for resumption.
    assert f.qsize() == 2  # the resumed 'a' plus the freshly added 'c'
