"""SQLite FTS5 full-text index of crawled documents."""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

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


_SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    id           INTEGER PRIMARY KEY,
    url          TEXT UNIQUE NOT NULL,
    title        TEXT,
    content      TEXT,
    content_type TEXT,
    crawled_at   REAL,
    depth        INTEGER,
    size         INTEGER
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
        self._ensure_fts5()
        self._conn.executescript(_SCHEMA)
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
    ) -> None:
        self._write(
            """
            INSERT INTO docs (url, title, content, content_type, crawled_at, depth, size)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                title=excluded.title,
                content=excluded.content,
                content_type=excluded.content_type,
                crawled_at=excluded.crawled_at,
                depth=excluded.depth,
                size=excluded.size
            """,
            (url, title, content, content_type, time.time(), depth, size),
        )

    def search(self, query: str, limit: int = 20, offset: int = 0) -> list[SearchHit]:
        match = _to_fts_query(query)
        if not match:
            return []
        rows = self._query(
            """
            SELECT
                d.url AS url,
                d.title AS title,
                d.content_type AS content_type,
                snippet(docs_fts, 1, :hl_open, :hl_close, ' … ', 16) AS snippet,
                bm25(docs_fts, 5.0, 1.0) AS score
            FROM docs_fts
            JOIN docs d ON d.id = docs_fts.rowid
            WHERE docs_fts MATCH :match
            ORDER BY score
            LIMIT :limit OFFSET :offset
            """,
            {
                "match": match,
                "limit": limit,
                "offset": offset,
                "hl_open": HL_OPEN,
                "hl_close": HL_CLOSE,
            },
        )
        return [
            SearchHit(
                url=r["url"],
                title=r["title"] or r["url"],
                content_type=r["content_type"] or "",
                snippet=r["snippet"] or "",
                score=r["score"],
            )
            for r in rows
        ]

    def count_matches(self, query: str) -> int:
        match = _to_fts_query(query)
        if not match:
            return 0
        rows = self._query(
            "SELECT COUNT(*) AS n FROM docs_fts WHERE docs_fts MATCH ?",
            (match,),
        )
        return int(rows[0]["n"])

    def stats(self) -> dict:
        total = self._query("SELECT COUNT(*) AS n FROM docs")[0]["n"]
        by_type = self._query(
            "SELECT content_type, COUNT(*) AS n FROM docs "
            "GROUP BY content_type ORDER BY n DESC"
        )
        return {
            "total_documents": int(total),
            "by_content_type": {r["content_type"]: int(r["n"]) for r in by_type},
        }

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

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _to_fts_query(raw: str) -> str:
    """Convert free user text into a safe FTS5 MATCH expression.

    Each word becomes a quoted term and they are AND-ed together, so a stray
    quote or FTS operator from the user can never break the query.
    """
    tokens = _TOKEN_RE.findall(raw or "")
    if not tokens:
        return ""
    return " AND ".join(f'"{t}"' for t in tokens)
