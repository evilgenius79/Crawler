"""SQLite FTS5 full-text index of crawled documents."""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)


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
"""


class Index:
    """A thin, synchronous wrapper over a SQLite FTS5 database.

    All calls are quick; the async crawler offloads writes with
    ``asyncio.to_thread`` so the event loop is never blocked.
    """

    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
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

    def upsert(
        self,
        url: str,
        title: str,
        content: str,
        content_type: str,
        depth: int,
        size: int,
    ) -> None:
        self._conn.execute(
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
        self._conn.commit()

    def search(self, query: str, limit: int = 20, offset: int = 0) -> list[SearchHit]:
        match = _to_fts_query(query)
        if not match:
            return []
        rows = self._conn.execute(
            """
            SELECT
                d.url AS url,
                d.title AS title,
                d.content_type AS content_type,
                snippet(docs_fts, 1, '<mark>', '</mark>', ' … ', 16) AS snippet,
                bm25(docs_fts, 5.0, 1.0) AS score
            FROM docs_fts
            JOIN docs d ON d.id = docs_fts.rowid
            WHERE docs_fts MATCH ?
            ORDER BY score
            LIMIT ? OFFSET ?
            """,
            (match, limit, offset),
        ).fetchall()
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
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM docs_fts WHERE docs_fts MATCH ?",
            (match,),
        ).fetchone()
        return int(row["n"])

    def stats(self) -> dict:
        total = self._conn.execute("SELECT COUNT(*) AS n FROM docs").fetchone()["n"]
        by_type = self._conn.execute(
            "SELECT content_type, COUNT(*) AS n FROM docs "
            "GROUP BY content_type ORDER BY n DESC"
        ).fetchall()
        return {
            "total_documents": int(total),
            "by_content_type": {r["content_type"]: int(r["n"]) for r in by_type},
        }

    def close(self) -> None:
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
