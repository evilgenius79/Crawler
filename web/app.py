"""FastAPI search front-end over the crawler index."""

from __future__ import annotations

import math
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from crawler.config import Config
from crawler.index import HL_CLOSE, HL_OPEN, Index

config = Config.load()
index = Index(config.db_path)

app = FastAPI(title="Personal Search", docs_url="/api/docs")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

PAGE_SIZE = 10


def _escaped_snippet_html(raw: str) -> str:
    """Escape crawled page text, then turn the index sentinels into <mark> tags.

    Escaping first neutralises any markup in the crawled text; the sentinels are
    ordinary private-use codepoints that survive escaping, so only our own
    trusted highlight tags remain. Safe to render as HTML (no XSS).
    """
    escaped = str(escape(raw or ""))
    return escaped.replace(HL_OPEN, "<mark>").replace(HL_CLOSE, "</mark>")


def safe_snippet(raw: str) -> Markup:
    """HTML-safe snippet for the server-rendered results page."""
    return Markup(_escaped_snippet_html(raw))


templates.env.filters["highlight"] = safe_snippet


@app.get("/", response_class=HTMLResponse)
def home(request: Request, q: str = "", page: int = Query(1, ge=1)):
    hits = []
    total = 0
    if q.strip():
        offset = (page - 1) * PAGE_SIZE
        hits = index.search(q, limit=PAGE_SIZE, offset=offset)
        total = index.count_matches(q)
    total_pages = max(1, math.ceil(total / PAGE_SIZE)) if total else 1
    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "q": q,
            "hits": hits,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "stats": index.stats(),
        },
    )


@app.get("/api/search")
def api_search(q: str = "", page: int = Query(1, ge=1)):
    offset = (page - 1) * PAGE_SIZE
    hits = index.search(q, limit=PAGE_SIZE, offset=offset) if q.strip() else []
    return JSONResponse(
        {
            "query": q,
            "page": page,
            "total": index.count_matches(q) if q.strip() else 0,
            "results": [
                {
                    "url": h.url,
                    "title": h.title,
                    "content_type": h.content_type,
                    # Page text is escaped and only our <mark> highlights are HTML,
                    # so this is safe for a consumer to drop into innerHTML.
                    "snippet": _escaped_snippet_html(h.snippet),
                    "score": h.score,
                }
                for h in hits
            ],
        }
    )


@app.get("/api/stats")
def api_stats():
    return index.stats()


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)
