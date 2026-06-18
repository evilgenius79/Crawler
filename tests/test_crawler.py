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


def test_index_filetype_filter():
    with tempfile.TemporaryDirectory() as d:
        idx = Index(os.path.join(d, "t.db"))
        idx.upsert("u1", "A", "python alpha", "text/html", 0, 1)
        idx.upsert("u2", "B", "python beta", "application/pdf", 0, 1)
        idx.upsert("u3", "C", "python gamma", "image/png", 0, 1)
        assert idx.count_matches("python") == 3
        assert idx.count_matches("python", content_type_like="application/pdf%") == 1
        hits = idx.search("python", content_type_like="image/%")
        assert len(hits) == 1 and hits[0].content_type == "image/png"
        idx.close()


def test_parse_query_filetype(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("CRAWLER_DATA_DIR", d)
        import importlib

        import web.app as app

        importlib.reload(app)
        clean, ftype, like, domain = app._parse_query("invoice type:pdf", "")
        assert clean == "invoice" and ftype == "pdf" and like == "application/pdf%"
        assert domain is None
        # A dropdown/chip selection works too.
        _, ftype2, like2, _ = app._parse_query("cat", "image")
        assert ftype2 == "image" and like2 == "image/%"
        # Unknown type token is left in the query, not treated as a filter.
        clean3, ftype3, like3, _ = app._parse_query("type:bogus hello", "")
        assert "type:bogus" in clean3 and ftype3 == "" and like3 is None
        # site: filter is extracted into a domain.
        clean4, _, _, domain4 = app._parse_query("docs site:example.com", "")
        assert clean4 == "docs" and domain4 == "example.com"


def test_looks_like_challenge_thin_vs_thick():
    from crawler.crawler import looks_like_challenge

    # Thin interstitial with a challenge title -> challenge.
    assert looks_like_challenge("Just a moment...", "text/html", "verifying")
    # A real long article that merely mentions the phrase -> NOT a challenge.
    long_body = "word " * 1000
    assert not looks_like_challenge("Checking your browser before accessing", "text/html", long_body)
    # Non-html never a challenge.
    assert not looks_like_challenge("Just a moment", "application/pdf", "")
    # Normal page.
    assert not looks_like_challenge("My Blog", "text/html", "hello")


def test_dedup_keeps_existing_url_updatable():
    with tempfile.TemporaryDirectory() as d:
        idx = Index(os.path.join(d, "t.db"))
        idx.upsert("http://a/1", "A", "shared body", "text/html", 0, 1, True)
        # New URL duplicating A is skipped.
        assert idx.upsert("http://b/2", "B", "different", "text/html", 0, 1, True) is True
        # b/2 re-crawled and its content now equals A's: must still UPDATE b/2.
        assert idx.upsert("http://b/2", "B2", "shared body", "text/html", 0, 1, True) is True
        doc = idx.get_document("http://b/2")
        assert doc["content"] == "shared body" and doc["title"] == "B2"
        idx.close()


def test_sitemap_parsing():
    import asyncio

    from crawler import sitemap

    class FakeResult:
        def __init__(self, body):
            self.ok = bool(body)
            self.body = body or b""

    pages = {
        "https://x.com/robots.txt": b"User-agent: *\nSitemap: https://x.com/sitemap.xml\n",
        "https://x.com/sitemap.xml": (
            b"<sitemapindex><sitemap><loc>https://x.com/sm1.xml</loc></sitemap></sitemapindex>"
        ),
        "https://x.com/sm1.xml": (
            b"<urlset><url><loc>https://x.com/a</loc></url>"
            b"<url><loc>https://x.com/b</loc></url></urlset>"
        ),
    }

    class FakeFetcher:
        async def fetch(self, url):
            return FakeResult(pages.get(url))

    urls = asyncio.run(sitemap.discover(FakeFetcher(), "https://x.com/"))
    assert "https://x.com/a" in urls and "https://x.com/b" in urls


def test_real_browser_ua_and_profile():
    from crawler.config import Config
    from crawler.render import RealBrowser

    cfg = Config()
    cfg.data_dir = "/tmp/whatever"
    rb = RealBrowser(cfg)
    # Default bot UA -> use the browser's own genuine UA (None).
    assert rb._user_agent() is None
    cfg.user_agent = "Mozilla/5.0 (Custom Browser)"
    assert rb._user_agent() == "Mozilla/5.0 (Custom Browser)"
    # Profile dir defaults under the data dir.
    assert rb._profile_dir().endswith("browser-profile")
    assert not rb.available  # nothing launched


def test_dedup_and_management():
    with tempfile.TemporaryDirectory() as d:
        idx = Index(os.path.join(d, "t.db"))
        assert idx.upsert("http://a/1", "T", "same body", "text/html", 0, 1, True) is True
        # Same text under a different URL is skipped as a duplicate.
        assert idx.upsert("http://b/2", "T", "same body", "text/html", 0, 1, True) is False
        # ...but allowed when dedup is off.
        assert idx.upsert("http://b/2", "T", "same body", "text/html", 0, 1, False) is True
        assert idx.stats()["total_documents"] == 2
        assert idx.stats()["total_bytes"] == 2

        # Management: delete by domain, delete by url, clear.
        idx.upsert("https://sub.x.com/p", "X", "hello world", "text/html", 0, 5, False)
        idx.upsert("https://x.com/q", "X", "hello there", "text/html", 0, 5, False)
        assert idx.delete_by_domain("x.com") == 2  # subdomain included
        idx.upsert("http://only/1", "O", "unique text", "text/html", 0, 1, False)
        assert idx.delete_url("http://only/1") == 1
        assert idx.clear_index() == 2  # the two http://a/1 and http://b/2 remain
        assert idx.stats()["total_documents"] == 0
        idx.close()


def test_settings_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        idx = Index(os.path.join(d, "t.db"))
        assert idx.get_setting("missing", {"x": 1}) == {"x": 1}
        idx.set_setting("schedule", {"enabled": True, "interval_hours": 6})
        assert idx.get_setting("schedule")["interval_hours"] == 6
        idx.close()


def test_crawl_history():
    with tempfile.TemporaryDirectory() as d:
        idx = Index(os.path.join(d, "t.db"))
        rid = idx.record_crawl_start(["https://x"], 100, 5)
        assert isinstance(rid, int)
        recent = idx.recent_crawls()
        assert len(recent) == 1
        assert recent[0]["status"] == "running"
        assert recent[0]["seeds"] == ["https://x"]

        idx.record_crawl_finish(rid, "finished", 10, 2)
        recent = idx.recent_crawls()
        assert recent[0]["status"] == "finished"
        assert recent[0]["pages_indexed"] == 10 and recent[0]["errors"] == 2

        # A leftover 'running' row from a crash is flagged on restart.
        idx.record_crawl_start(["https://y"], 1, 1)
        assert idx.mark_running_interrupted() == 1
        assert all(r["status"] != "running" for r in idx.recent_crawls())
        idx.close()


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
