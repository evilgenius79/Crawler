"""FastAPI search front-end over the crawler index."""

from __future__ import annotations

import math
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from crawler.config import Config
from crawler.index import Index

config = Config.load()
index = Index(config.db_path)

app = FastAPI(title="Personal Search", docs_url="/api/docs")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

PAGE_SIZE = 10


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
                    "snippet": h.snippet,
                    "score": h.score,
                }
                for h in hits
            ],
        }
    )


@app.get("/api/stats")
def api_stats():
    return index.stats()
