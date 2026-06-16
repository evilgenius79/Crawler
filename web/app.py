"""FastAPI search front-end + admin dashboard over the crawler index."""

from __future__ import annotations

import math
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from crawler.config import Config
from crawler.index import HL_CLOSE, HL_OPEN, Index

from .manager import CrawlManager

config = Config.load()
index = Index(config.db_path)
manager = CrawlManager(config)


@asynccontextmanager
async def lifespan(app: FastAPI):
    autostart = os.environ.get("CRAWLER_AUTOSTART", "").lower() in ("1", "true", "yes")
    if autostart and config.seeds:
        try:
            await manager.start(config.seeds, {})
        except Exception:  # pragma: no cover - best effort
            pass
    yield


app = FastAPI(title="Personal Search", docs_url="/api/docs", lifespan=lifespan)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

PAGE_SIZE = 10

# Friendly file-type filters -> SQLite LIKE patterns over the stored content type.
FILETYPE_FILTERS: dict[str, str] = {
    "web": "text/html%",
    "pdf": "application/pdf%",
    "doc": "%word%",
    "text": "text/%",
    "image": "image/%",
    "audio": "audio/%",
    "video": "video/%",
    "archive": "application/%zip%",
}

# The chips shown on the search page, in order.
FILETYPE_CHIPS = [
    ("", "All"),
    ("web", "Web pages"),
    ("pdf", "PDF"),
    ("doc", "Docs"),
    ("image", "Images"),
    ("text", "Text"),
    ("video", "Video"),
    ("audio", "Audio"),
]


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


def _parse_query(q: str, ftype: str) -> tuple[str, str, str | None]:
    """Pull an inline `type:pdf` token out of the query and resolve the filter.

    Returns (clean_query, effective_type, like_pattern).
    """
    terms = []
    for tok in q.split():
        low = tok.lower()
        if low.startswith("type:") and low[5:] in FILETYPE_FILTERS:
            ftype = low[5:]
        else:
            terms.append(tok)
    like = FILETYPE_FILTERS.get(ftype)
    return " ".join(terms), ftype, like


@app.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    q: str = "",
    type: str = "",
    page: int = Query(1, ge=1),
):
    clean_q, ftype, like = _parse_query(q, type)
    hits = []
    total = 0
    if clean_q.strip():
        offset = (page - 1) * PAGE_SIZE
        hits = index.search(clean_q, limit=PAGE_SIZE, offset=offset, content_type_like=like)
        total = index.count_matches(clean_q, content_type_like=like)
    total_pages = max(1, math.ceil(total / PAGE_SIZE)) if total else 1
    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "q": q,
            "ftype": ftype,
            "chips": FILETYPE_CHIPS,
            "hits": hits,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "stats": index.stats(),
            "running": manager.running,
        },
    )


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request):
    return templates.TemplateResponse(
        request,
        "admin.html",
        {"stats": index.stats(), "config": config},
    )


# ----------------------------- JSON API ---------------------------------- #
@app.get("/api/search")
def api_search(q: str = "", type: str = "", page: int = Query(1, ge=1)):
    clean_q, ftype, like = _parse_query(q, type)
    offset = (page - 1) * PAGE_SIZE
    hits = (
        index.search(clean_q, limit=PAGE_SIZE, offset=offset, content_type_like=like)
        if clean_q.strip()
        else []
    )
    return JSONResponse(
        {
            "query": q,
            "type": ftype,
            "page": page,
            "total": index.count_matches(clean_q, content_type_like=like) if clean_q.strip() else 0,
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


@app.get("/api/crawl/status")
def api_crawl_status():
    st = manager.status()
    st["stats"] = index.stats()
    return st


@app.post("/api/crawl/start")
async def api_crawl_start(payload: dict = Body(default={})):
    seeds = _as_url_list(payload.get("seeds") or payload.get("urls"))
    overrides = _overrides(payload)
    try:
        await manager.start(seeds, overrides)
    except (RuntimeError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return {"ok": True, "status": manager.status()}


@app.post("/api/crawl/add")
async def api_crawl_add(payload: dict = Body(default={})):
    urls = _as_url_list(payload.get("urls") or payload.get("seeds"))
    overrides = _overrides(payload)
    try:
        result = await manager.add_urls(urls, overrides)
    except (RuntimeError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return {"ok": True, "result": result, "status": manager.status()}


@app.post("/api/crawl/stop")
def api_crawl_stop():
    stopped = manager.stop()
    return {"ok": True, "stopping": stopped, "status": manager.status()}


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)


# ----------------------------- helpers ----------------------------------- #
def _as_url_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        # Accept newline- or comma-separated text from a textarea.
        return [p.strip() for p in value.replace(",", "\n").splitlines() if p.strip()]
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def _to_bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _overrides(payload: dict) -> dict:
    out: dict = {}
    # (caster, minimum) — clamp so a bad value can't wedge a crawl
    # (e.g. concurrency 0 would spawn no workers and hang forever).
    numeric = {
        "max_pages": (int, 1),
        "max_depth": (int, 0),
        "concurrency": (int, 1),
        "politeness_delay": (float, 0.0),
    }
    for key, (caster, low) in numeric.items():
        if payload.get(key) not in (None, ""):
            try:
                out[key] = max(low, caster(payload[key]))
            except (TypeError, ValueError):
                pass
    for key in ("same_domain_only", "respect_robots", "render_js"):
        if key in payload and payload[key] is not None:
            out[key] = _to_bool(payload[key])
    return out
