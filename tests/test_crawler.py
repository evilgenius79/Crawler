"""Unit tests that need no network access."""

from __future__ import annotations

import os
import tempfile

from crawler.extractors import extract
from crawler.index import Index, _to_fts_query
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
