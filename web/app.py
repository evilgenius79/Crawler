"""FastAPI search front-end + admin dashboard over the crawler index."""

from __future__ import annotations

import logging
import math
import os
import secrets
import sys
from contextlib import asynccontextmanager
from pathlib import Path

log = logging.getLogger("crawler.web")


def playwright_available() -> bool:
    try:
        import playwright  # noqa: F401

        return True
    except Exception:
        return False


def _log_environment() -> None:
    """Print which Python this server runs on + whether Playwright is visible.

    This makes the #1 real-browser gotcha obvious: Playwright installed into a
    different Python than the one running the server.
    """
    log.info("Server Python: %s", sys.executable)
    if playwright_available():
        log.info("Playwright: available — real-browser / render-JS ready.")
    else:
        log.warning(
            "Playwright: NOT installed in THIS environment, so real-browser / "
            "render-JS will fall back to plain HTTP. Install it into the Python "
            "above, e.g.:  \"%s\" -m pip install playwright && \"%s\" -m playwright "
            "install chromium",
            sys.executable, sys.executable,
        )

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from crawler.config import Config
from crawler.index import HL_CLOSE, HL_OPEN, Index
from crawler.utils import domain_of

from .manager import CrawlManager
from .scheduler import Scheduler

config = Config.load()
index = Index(config.db_path)
manager = CrawlManager(config, index)
scheduler = Scheduler(manager, index)

# Optional admin password. When unset, the admin controls are open (handy for a
# trusted LAN); set CRAWLER_ADMIN_PASSWORD to lock down crawl/index controls.
ADMIN_PASSWORD = os.environ.get("CRAWLER_ADMIN_PASSWORD", "")
_basic = HTTPBasic(auto_error=False)


def require_admin(credentials: HTTPBasicCredentials | None = Depends(_basic)) -> None:
    if not ADMIN_PASSWORD:
        return
    # Compare as bytes — compare_digest raises on non-ASCII str operands.
    ok = credentials is not None and secrets.compare_digest(
        credentials.password.encode("utf-8"), ADMIN_PASSWORD.encode("utf-8")
    )
    if not ok:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": 'Basic realm="PersonalSearch admin"'},
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # A crawl row left as 'running' means the server died mid-crawl last time.
    index.mark_running_interrupted()
    _log_environment()
    autostart = os.environ.get("CRAWLER_AUTOSTART", "").lower() in ("1", "true", "yes")
    if autostart and config.seeds:
        try:
            await manager.start(config.seeds, {})
        except Exception:  # pragma: no cover - best effort
            pass
    scheduler.start()
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
    """Escape crawled page text, then turn the index sentinels into <mark> tags."""
    escaped = str(escape(raw or ""))
    return escaped.replace(HL_OPEN, "<mark>").replace(HL_CLOSE, "</mark>")


def safe_snippet(raw: str) -> Markup:
    return Markup(_escaped_snippet_html(raw))


def _fmt_ts(ts) -> str:
    import datetime

    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return ""


templates.env.filters["highlight"] = safe_snippet
templates.env.filters["domain"] = domain_of
templates.env.filters["ts"] = _fmt_ts


def _parse_query(q: str, ftype: str) -> tuple[str, str, str | None, str | None]:
    """Pull inline `type:` and `site:` tokens out of the query.

    Returns (clean_query, effective_type, like_pattern, domain).
    """
    terms = []
    domain = None
    for tok in q.split():
        low = tok.lower()
        if low.startswith("type:") and low[5:] in FILETYPE_FILTERS:
            ftype = low[5:]
        elif low.startswith("site:") and len(low) > 5:
            domain = low[5:].strip("/")
        else:
            terms.append(tok)
    like = FILETYPE_FILTERS.get(ftype)
    return " ".join(terms), ftype, like, domain


@app.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    q: str = "",
    type: str = "",
    sort: str = "relevance",
    page: int = Query(1, ge=1),
):
    clean_q, ftype, like, domain = _parse_query(q, type)
    offset = (page - 1) * PAGE_SIZE
    hits, total = [], 0
    if clean_q.strip():
        hits = index.search(
            clean_q, limit=PAGE_SIZE, offset=offset,
            content_type_like=like, domain=domain, sort=sort,
        )
        total = index.count_matches(clean_q, content_type_like=like, domain=domain)
    elif domain:
        # Browse a whole domain (e.g. site:example.com with no search terms).
        hits = index.list_by_domain(domain, limit=PAGE_SIZE, offset=offset, sort=sort)
        total = index.count_by_domain(domain)
    total_pages = max(1, math.ceil(total / PAGE_SIZE)) if total else 1
    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "q": q, "ftype": ftype, "sort": sort, "chips": FILETYPE_CHIPS,
            "hits": hits, "total": total, "page": page, "total_pages": total_pages,
            "stats": index.stats(), "running": manager.running,
        },
    )


@app.get("/cached", response_class=HTMLResponse)
def cached(request: Request, url: str):
    doc = index.get_document(url)
    if not doc:
        raise HTTPException(status_code=404, detail="Not in the index")
    return templates.TemplateResponse(request, "cached.html", {"doc": doc})


@app.get("/stats", response_class=HTMLResponse)
def stats_page(request: Request):
    return templates.TemplateResponse(
        request,
        "stats.html",
        {"stats": index.stats(), "domains": index.top_domains(25)},
    )


@app.get("/admin", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def admin(request: Request):
    return templates.TemplateResponse(
        request, "admin.html", {"stats": index.stats(), "config": config}
    )


# ----------------------------- JSON API ---------------------------------- #
@app.get("/api/search")
def api_search(q: str = "", type: str = "", sort: str = "relevance", page: int = Query(1, ge=1)):
    clean_q, ftype, like, domain = _parse_query(q, type)
    offset = (page - 1) * PAGE_SIZE
    if clean_q.strip():
        hits = index.search(clean_q, limit=PAGE_SIZE, offset=offset,
                            content_type_like=like, domain=domain, sort=sort)
        total = index.count_matches(clean_q, content_type_like=like, domain=domain)
    elif domain:
        hits = index.list_by_domain(domain, limit=PAGE_SIZE, offset=offset, sort=sort)
        total = index.count_by_domain(domain)
    else:
        hits, total = [], 0
    return JSONResponse(
        {
            "query": q, "type": ftype, "page": page,
            "total": total,
            "results": [
                {
                    "url": h.url, "title": h.title, "content_type": h.content_type,
                    "crawled_at": h.crawled_at,
                    "snippet": _escaped_snippet_html(h.snippet), "score": h.score,
                }
                for h in hits
            ],
        }
    )


@app.get("/api/stats")
def api_stats():
    return {**index.stats(), "top_domains": index.top_domains(25)}


@app.get("/api/crawl/status")
def api_crawl_status():
    st = manager.status()
    st["stats"] = index.stats()
    st["recent"] = manager.history()
    st["schedule"] = scheduler.status()
    st["playwright"] = playwright_available()
    # Show the latest run's *persisted* errors so the panel works live AND after
    # a restart (the in-memory list is lost when the process restarts).
    if st["recent"]:
        st["recent_errors"] = index.errors_for_run(st["recent"][0]["id"], limit=30)
    return st


@app.get("/api/crawl/history")
def api_crawl_history(limit: int = 25):
    return {"recent": manager.history(limit)}


@app.get("/api/crawl/errors")
def api_crawl_errors(run_id: int):
    return {"run_id": run_id, "errors": index.errors_for_run(run_id)}


@app.post("/api/crawl/start", dependencies=[Depends(require_admin)])
async def api_crawl_start(payload: dict = Body(default={})):
    seeds = _as_url_list(payload.get("seeds") or payload.get("urls"))
    try:
        await manager.start(seeds, _overrides(payload))
    except (RuntimeError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return {"ok": True, "status": manager.status()}


@app.post("/api/crawl/add", dependencies=[Depends(require_admin)])
async def api_crawl_add(payload: dict = Body(default={})):
    urls = _as_url_list(payload.get("urls") or payload.get("seeds"))
    try:
        result = await manager.add_urls(urls, _overrides(payload))
    except (RuntimeError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return {"ok": True, "result": result, "status": manager.status()}


@app.post("/api/crawl/stop", dependencies=[Depends(require_admin)])
def api_crawl_stop():
    return {"ok": True, "stopping": manager.stop(), "status": manager.status()}


@app.post("/api/crawl/test", dependencies=[Depends(require_admin)])
async def api_crawl_test(payload: dict = Body(default={})):
    url = str(payload.get("url", "")).strip()
    if not url:
        return JSONResponse({"ok": False, "error": "No URL given."}, status_code=400)
    try:
        return await manager.test_url(url, _overrides(payload))
    except (RuntimeError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


# ----------------------------- scheduler --------------------------------- #
@app.get("/api/schedule")
def api_schedule_get():
    return scheduler.status()


@app.post("/api/schedule", dependencies=[Depends(require_admin)])
async def api_schedule_set(payload: dict = Body(default={})):
    changes: dict = {}
    if "enabled" in payload:
        changes["enabled"] = _to_bool(payload["enabled"])
    for key in ("interval_hours", "older_than_days"):
        if payload.get(key) not in (None, ""):
            try:
                changes[key] = float(payload[key])
            except (TypeError, ValueError):
                pass
    if "seeds" in payload:
        changes["seeds"] = _as_url_list(payload["seeds"])
    return {"ok": True, "schedule": await scheduler.apply(changes)}


# -------------------------- index management ----------------------------- #
@app.post("/api/index/clear", dependencies=[Depends(require_admin)])
def api_index_clear():
    if manager.running:
        return JSONResponse(
            {"ok": False, "error": "Stop the running crawl first."}, status_code=400
        )
    return {"ok": True, "deleted": index.clear_index()}


@app.post("/api/index/delete-domain", dependencies=[Depends(require_admin)])
def api_index_delete_domain(payload: dict = Body(default={})):
    if manager.running:
        return JSONResponse(
            {"ok": False, "error": "Stop the running crawl first."}, status_code=400
        )
    domain = str(payload.get("domain", "")).strip().lower()
    if not domain:
        return JSONResponse({"ok": False, "error": "No domain given."}, status_code=400)
    return {"ok": True, "deleted": index.delete_by_domain(domain)}


@app.post("/api/index/delete-url", dependencies=[Depends(require_admin)])
def api_index_delete_url(payload: dict = Body(default={})):
    if manager.running:
        return JSONResponse(
            {"ok": False, "error": "Stop the running crawl first."}, status_code=400
        )
    url = str(payload.get("url", "")).strip()
    if not url:
        return JSONResponse({"ok": False, "error": "No URL given."}, status_code=400)
    return {"ok": True, "deleted": index.delete_url(url)}


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)


# ----------------------------- helpers ----------------------------------- #
def _as_url_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
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
    # (caster, minimum) — clamp so a bad value can't wedge a crawl.
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
    for key in ("same_domain_only", "respect_robots", "render_js", "real_browser", "deduplicate"):
        if key in payload and payload[key] is not None:
            out[key] = _to_bool(payload[key])
    if "exclude_patterns" in payload and payload["exclude_patterns"] is not None:
        out["exclude_patterns"] = _as_url_list(payload["exclude_patterns"])
    ua = payload.get("user_agent")
    if isinstance(ua, str) and ua.strip():
        out["user_agent"] = ua.strip()
    return out
