"""SQLite FTS5 full-text index of crawled documents."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .utils import domain_of

_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)

# Snippet highlight markers. We deliberately use Unicode private-use characters
# (which never appear in real content) instead of literal "<mark>" tags. The
# presentation layer escapes the surrounding text and only THEN swaps these for
# real HTML, so attacker-controlled page text can't inject markup (XSS).
HL_OPEN = ""
HL_CLOSE = ""


@dataclass
class SearchHit:
    url: str
    title: str
    content_type: str
    snippet: str
    score: float
    crawled_at: float = 0.0


_SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    id           INTEGER PRIMARY KEY,
    url          TEXT UNIQUE NOT NULL,
    title        TEXT,
    content      TEXT,
    content_type TEXT,
    crawled_at   REAL,
    depth        INTEGER,
    size         INTEGER,
    domain       TEXT,
    content_hash TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
    title,
    content,
    content='docs',
    content_rowid='id',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS docs_ai AFTER INSERT ON docs BEGIN
    INSERT INTO docs_fts(rowid, title, content)
    VALUES (new.id, new.title, new.content);
END;

CREATE TRIGGER IF NOT EXISTS docs_ad AFTER DELETE ON docs BEGIN
    INSERT INTO docs_fts(docs_fts, rowid, title, content)
    VALUES ('delete', old.id, old.title, old.content);
END;

CREATE TRIGGER IF NOT EXISTS docs_au AFTER UPDATE ON docs BEGIN
    INSERT INTO docs_fts(docs_fts, rowid, title, content)
    VALUES ('delete', old.id, old.title, old.content);
    INSERT INTO docs_fts(rowid, title, content)
    VALUES (new.id, new.title, new.content);
END;

-- The crawl frontier, persisted so crawls can resume and re-crawl on a schedule.
CREATE TABLE IF NOT EXISTS frontier (
    url        TEXT PRIMARY KEY,
    depth      INTEGER NOT NULL,
    state      TEXT NOT NULL DEFAULT 'pending',  -- pending | done | error
    updated_at REAL
);
CREATE INDEX IF NOT EXISTS frontier_state ON frontier(state);

-- History of crawl runs, shown in the admin dashboard.
CREATE TABLE IF NOT EXISTS crawl_runs (
    id            INTEGER PRIMARY KEY,
    started_at    REAL,
    finished_at   REAL,
    seeds         TEXT,
    status        TEXT,   -- running | finished | stopped | error | interrupted
    pages_indexed INTEGER DEFAULT 0,
    errors        INTEGER DEFAULT 0,
    max_pages     INTEGER,
    max_depth     INTEGER,
    detail        TEXT
);

-- Per-run fetch errors, so a past crawl's failures can be reviewed.
CREATE TABLE IF NOT EXISTS crawl_errors (
    id     INTEGER PRIMARY KEY,
    run_id INTEGER,
    url    TEXT,
    reason TEXT,
    ts     REAL
);
CREATE INDEX IF NOT EXISTS crawl_errors_run ON crawl_errors(run_id);

-- Persisted key/value settings (e.g. the built-in scheduler config).
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class Index:
    """A thin, synchronous wrapper over a SQLite FTS5 database.

    All calls are quick; the async crawler offloads writes with
    ``asyncio.to_thread`` so the event loop is never blocked.
    """

    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        # The connection is shared across crawler worker threads (via
        # asyncio.to_thread), so every access is serialised with this lock.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        # Wait rather than erroring if another connection (e.g. a running crawl)
        # holds the write lock momentarily.
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._ensure_fts5()
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        """Add newer columns to a docs table created by an older version."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(docs)")}
        if "domain" not in cols:
            self._conn.execute("ALTER TABLE docs ADD COLUMN domain TEXT")
        if "content_hash" not in cols:
            self._conn.execute("ALTER TABLE docs ADD COLUMN content_hash TEXT")
        # Created here (not in the schema) so they work for migrated old DBs too.
        self._conn.execute("CREATE INDEX IF NOT EXISTS docs_domain ON docs(domain)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS docs_chash ON docs(content_hash)")
        self._conn.commit()
        # Idempotent backfill: fill any rows still missing the new columns. This
        # also completes a migration that was interrupted partway through.
        while True:
            rows = self._conn.execute(
                "SELECT id, url, content FROM docs "
                "WHERE domain IS NULL OR content_hash IS NULL LIMIT 1000"
            ).fetchall()
            if not rows:
                break
            for row in rows:
                self._conn.execute(
                    "UPDATE docs SET domain=?, content_hash=? WHERE id=?",
                    (domain_of(row["url"]), _content_hash(row["content"] or ""), row["id"]),
                )
            self._conn.commit()

    def _ensure_fts5(self) -> None:
        try:
            self._conn.execute("CREATE VIRTUAL TABLE _fts_probe USING fts5(x)")
            self._conn.execute("DROP TABLE _fts_probe")
        except sqlite3.OperationalError as exc:  # pragma: no cover
            raise RuntimeError(
                "Your SQLite build lacks the FTS5 extension, which this index "
                "requires. Use a Python built against a modern SQLite (the "
                "provided Docker image works out of the box)."
            ) from exc

    # -- locked low-level helpers ------------------------------------- #
    def _query(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _write(self, sql: str, params=()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    # ----------------------------------------------------------------- #
    def upsert(
        self,
        url: str,
        title: str,
        content: str,
        content_type: str,
        depth: int,
        size: int,
        deduplicate: bool = False,
    ) -> bool:
        """Insert or update a document. Returns False if skipped as a duplicate.

        With ``deduplicate`` on, a page whose exact text already exists under a
        *different* URL is skipped, so mirrors/boilerplate don't bloat the index.
        """
        domain = domain_of(url)
        chash = _content_hash(content) if content else ""
        with self._lock:
            if deduplicate and chash:
                # Only drop a *new* URL that duplicates an existing one. A URL
                # already in the index must always update, even if its new
                # content now matches another page (otherwise it keeps stale text).
                known = self._conn.execute(
                    "SELECT 1 FROM docs WHERE url=? LIMIT 1", (url,)
                ).fetchone()
                if not known:
                    dup = self._conn.execute(
                        "SELECT 1 FROM docs WHERE content_hash=? AND url<>? LIMIT 1",
                        (chash, url),
                    ).fetchone()
                    if dup:
                        return False
            self._conn.execute(
                """
                INSERT INTO docs
                    (url, title, content, content_type, crawled_at, depth, size, domain, content_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    title=excluded.title,
                    content=excluded.content,
                    content_type=excluded.content_type,
                    crawled_at=excluded.crawled_at,
                    depth=excluded.depth,
                    size=excluded.size,
                    domain=excluded.domain,
                    content_hash=excluded.content_hash
                """,
                (url, title, content, content_type, time.time(), depth, size, domain, chash),
            )
            self._conn.commit()
        return True

    @staticmethod
    def _filters(params: dict, content_type_like: str | None, domain: str | None) -> str:
        clause = ""
        if content_type_like:
            clause += " AND d.content_type LIKE :ctype"
            params["ctype"] = content_type_like
        if domain:
            clause += " AND (d.domain = :dom OR d.domain LIKE :domsub)"
            params["dom"] = domain
            params["domsub"] = "%." + domain
        return clause

    def search(
        self,
        query: str,
        limit: int = 20,
        offset: int = 0,
        content_type_like: str | None = None,
        domain: str | None = None,
        sort: str = "relevance",
    ) -> list[SearchHit]:
        match = _to_fts_query(query)
        if not match:
            return []
        params = {
            "match": match,
            "limit": limit,
            "offset": offset,
            "hl_open": HL_OPEN,
            "hl_close": HL_CLOSE,
        }
        filters = self._filters(params, content_type_like, domain)
        order = "d.crawled_at DESC" if sort == "date" else "score"
        rows = self._query(
            f"""
            SELECT
                d.url AS url,
                d.title AS title,
                d.content_type AS content_type,
                d.crawled_at AS crawled_at,
                snippet(docs_fts, 1, :hl_open, :hl_close, ' … ', 16) AS snippet,
                bm25(docs_fts, 5.0, 1.0) AS score
            FROM docs_fts
            JOIN docs d ON d.id = docs_fts.rowid
            WHERE docs_fts MATCH :match{filters}
            ORDER BY {order}
            LIMIT :limit OFFSET :offset
            """,
            params,
        )
        return [
            SearchHit(
                url=r["url"],
                title=r["title"] or r["url"],
                content_type=r["content_type"] or "",
                snippet=r["snippet"] or "",
                score=r["score"],
                crawled_at=r["crawled_at"] or 0.0,
            )
            for r in rows
        ]

    def count_matches(
        self,
        query: str,
        content_type_like: str | None = None,
        domain: str | None = None,
    ) -> int:
        match = _to_fts_query(query)
        if not match:
            return 0
        params = {"match": match}
        filters = self._filters(params, content_type_like, domain)
        rows = self._query(
            f"""
            SELECT COUNT(*) AS n
            FROM docs_fts JOIN docs d ON d.id = docs_fts.rowid
            WHERE docs_fts MATCH :match{filters}
            """,
            params,
        )
        return int(rows[0]["n"])

    def list_by_domain(
        self, domain: str, limit: int = 20, offset: int = 0, sort: str = "relevance"
    ) -> list[SearchHit]:
        """List a domain's pages without a text query (for browsing)."""
        order = "crawled_at DESC" if sort == "date" else "url"
        rows = self._query(
            f"""SELECT url, title, content_type, crawled_at FROM docs
                WHERE domain=? OR domain LIKE ?
                ORDER BY {order} LIMIT ? OFFSET ?""",
            (domain, "%." + domain, limit, offset),
        )
        return [
            SearchHit(
                url=r["url"], title=r["title"] or r["url"],
                content_type=r["content_type"] or "", snippet="",
                score=0.0, crawled_at=r["crawled_at"] or 0.0,
            )
            for r in rows
        ]

    def count_by_domain(self, domain: str) -> int:
        rows = self._query(
            "SELECT COUNT(*) AS n FROM docs WHERE domain=? OR domain LIKE ?",
            (domain, "%." + domain),
        )
        return int(rows[0]["n"])

    def get_document(self, url: str) -> dict | None:
        rows = self._query(
            "SELECT url, title, content, content_type, crawled_at, domain FROM docs WHERE url=?",
            (url,),
        )
        return dict(rows[0]) if rows else None

    def stats(self) -> dict:
        row = self._query("SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS sz FROM docs")[0]
        by_type = self._query(
            "SELECT content_type, COUNT(*) AS n FROM docs "
            "GROUP BY content_type ORDER BY n DESC"
        )
        return {
            "total_documents": int(row["n"]),
            "total_bytes": int(row["sz"]),
            "by_content_type": {r["content_type"]: int(r["n"]) for r in by_type},
        }

    def top_domains(self, limit: int = 20) -> list[dict]:
        rows = self._query(
            "SELECT domain, COUNT(*) AS n FROM docs WHERE domain IS NOT NULL AND domain<>'' "
            "GROUP BY domain ORDER BY n DESC LIMIT ?",
            (limit,),
        )
        return [{"domain": r["domain"], "count": int(r["n"])} for r in rows]

    # ------------------------------------------------------------------ #
    # Index management (admin)
    # ------------------------------------------------------------------ #
    def clear_index(self) -> int:
        """Delete every document and reset the frontier. Keeps crawl history."""
        with self._lock:
            n = self._conn.execute("SELECT COUNT(*) AS n FROM docs").fetchone()["n"]
            self._conn.execute("DELETE FROM docs")
            self._conn.execute("DELETE FROM frontier")
            self._conn.commit()
        return int(n)

    def delete_by_domain(self, domain: str) -> int:
        domain = domain.strip().lower()
        # Only accept real domain characters, so a stray LIKE wildcard ('%','_')
        # can never turn a single-domain delete into a wipe of the whole index.
        if not domain or not re.fullmatch(r"[a-z0-9.\-]+", domain):
            return 0
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM docs WHERE domain=? OR domain LIKE ?",
                (domain, "%." + domain),
            )
            self._conn.execute(
                "DELETE FROM frontier WHERE url LIKE ? OR url LIKE ?",
                (f"%://{domain}/%", f"%.{domain}/%"),
            )
            self._conn.commit()
            return cur.rowcount

    def delete_url(self, url: str) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM docs WHERE url=?", (url,))
            self._conn.execute("DELETE FROM frontier WHERE url=?", (url,))
            self._conn.commit()
            return cur.rowcount

    # ------------------------------------------------------------------ #
    # Key/value settings
    # ------------------------------------------------------------------ #
    def get_setting(self, key: str, default=None):
        rows = self._query("SELECT value FROM settings WHERE key=?", (key,))
        if not rows:
            return default
        try:
            return json.loads(rows[0]["value"])
        except (TypeError, ValueError):
            return default

    def set_setting(self, key: str, value) -> None:
        self._write(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    # ------------------------------------------------------------------ #
    # Frontier persistence (used for resumable + scheduled crawls)
    # ------------------------------------------------------------------ #
    def frontier_add_many(self, rows: list[tuple[str, int]]) -> None:
        """Insert pending URLs, ignoring any we already know about."""
        if not rows:
            return
        now = time.time()
        with self._lock:
            self._conn.executemany(
                "INSERT OR IGNORE INTO frontier (url, depth, state, updated_at) "
                "VALUES (?, ?, 'pending', ?)",
                [(url, depth, now) for url, depth in rows],
            )
            self._conn.commit()

    def frontier_mark(self, url: str, state: str) -> None:
        self._write(
            "UPDATE frontier SET state = ?, updated_at = ? WHERE url = ?",
            (state, time.time(), url),
        )

    def frontier_known_urls(self) -> set[str]:
        return {r["url"] for r in self._query("SELECT url FROM frontier")}

    def frontier_pending(self) -> list[tuple[str, int]]:
        rows = self._query("SELECT url, depth FROM frontier WHERE state = 'pending'")
        return [(r["url"], r["depth"]) for r in rows]

    def requeue_stale(self, older_than_seconds: float) -> int:
        """Mark documents older than a threshold as pending for re-crawl.

        Returns the number of URLs re-queued. Used for incremental crawling.
        """
        cutoff = time.time() - older_than_seconds
        with self._lock:
            urls = self._conn.execute(
                "SELECT url, depth FROM docs WHERE crawled_at < ?", (cutoff,)
            ).fetchall()
            rows = [(r["url"], r["depth"] or 0) for r in urls]
            for url, depth in rows:
                self._conn.execute(
                    "INSERT INTO frontier (url, depth, state, updated_at) "
                    "VALUES (?, ?, 'pending', ?) "
                    "ON CONFLICT(url) DO UPDATE SET state='pending', updated_at=excluded.updated_at",
                    (url, depth, time.time()),
                )
            self._conn.commit()
        return len(rows)

    # ------------------------------------------------------------------ #
    # Crawl-run history (shown in the admin dashboard)
    # ------------------------------------------------------------------ #
    def record_crawl_start(
        self, seeds: list[str], max_pages: int, max_depth: int
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO crawl_runs "
                "(started_at, seeds, status, max_pages, max_depth) "
                "VALUES (?, ?, 'running', ?, ?)",
                (time.time(), json.dumps(seeds), max_pages, max_depth),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def record_crawl_finish(
        self,
        run_id: int,
        status: str,
        pages_indexed: int,
        errors: int,
        detail: str | None = None,
    ) -> None:
        self._write(
            "UPDATE crawl_runs SET finished_at=?, status=?, pages_indexed=?, "
            "errors=?, detail=? WHERE id=?",
            (time.time(), status, pages_indexed, errors, detail, run_id),
        )

    def add_crawl_error(self, run_id: int, url: str, reason: str) -> None:
        self._write(
            "INSERT INTO crawl_errors (run_id, url, reason, ts) VALUES (?, ?, ?, ?)",
            (run_id, url, reason, time.time()),
        )

    def errors_for_run(self, run_id: int, limit: int = 200) -> list[dict]:
        rows = self._query(
            "SELECT url, reason, ts FROM crawl_errors WHERE run_id=? "
            "ORDER BY id DESC LIMIT ?",
            (run_id, limit),
        )
        return [{"url": r["url"], "reason": r["reason"], "when": r["ts"]} for r in rows]

    def mark_running_interrupted(self) -> int:
        """On startup, flag any 'running' rows left over from a crash/restart."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE crawl_runs SET status='interrupted', finished_at=? "
                "WHERE status='running'",
                (time.time(),),
            )
            self._conn.commit()
            return cur.rowcount

    def recent_crawls(self, limit: int = 12) -> list[dict]:
        rows = self._query(
            "SELECT * FROM crawl_runs ORDER BY started_at DESC LIMIT ?", (limit,)
        )
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["seeds"] = json.loads(d.get("seeds") or "[]")
            except (TypeError, ValueError):
                d["seeds"] = []
            out.append(d)
        return out

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _content_hash(text: str) -> str:
    """A stable hash of page text, used for exact-duplicate detection."""
    return hashlib.sha1(text.strip().encode("utf-8", "replace")).hexdigest()


def _to_fts_query(raw: str) -> str:
    """Convert free user text into a safe FTS5 MATCH expression.

    Each word becomes a quoted term and they are AND-ed together, so a stray
    quote or FTS operator from the user can never break the query.
    """
    tokens = _TOKEN_RE.findall(raw or "")
    if not tokens:
        return ""
    return " AND ".join(f'"{t}"' for t in tokens)
