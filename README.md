# PersonalSearch — a personal web crawler & search engine

A small, fast, self-hosted crawler and full-text search index. Point it at some
seed URLs, let it crawl, then search your own corner of the web from a clean,
ad-free, Google-style UI. Designed to run happily on an unraid box (or any
Docker host).

- **Async crawler** (aiohttp) — many pages in flight at once, polite per host
- **Crawls everything** — HTML, PDF, DOCX and plain text get full-text indexed;
  every other file type is still indexed by URL/type/size so nothing is lost
- **Zero external services** — the index is a single SQLite file using FTS5
- **BM25 ranked search** with highlighted snippets, a JSON API, and a web UI
- **robots.txt aware**, with configurable politeness, scope and depth limits

> Built for personal/research use. Crawl responsibly: set a real `user_agent`,
> keep `respect_robots: true`, and be considerate with `politeness_delay`.

## Architecture

```
seeds ─▶ Frontier ─▶ Fetcher ─▶ Extractors ─▶ Index (SQLite FTS5)
            ▲           │            │
            └── links ──┴────────────┘            Web UI / CLI ─▶ Index
```

| Module | Job |
|--------|-----|
| `crawler/frontier.py`   | De-duplicated BFS queue of URLs |
| `crawler/fetcher.py`    | Async HTTP with timeout + size cap |
| `crawler/robots.py`     | Cached robots.txt rules & crawl-delay |
| `crawler/extractors.py` | HTML/PDF/DOCX/text → title + text + links |
| `crawler/index.py`      | SQLite FTS5 store, upsert + ranked search |
| `crawler/crawler.py`    | Orchestrates workers, scope and politeness |
| `web/app.py`            | FastAPI search UI + JSON API |

## Quick start (local)

```bash
pip install -r requirements.txt

# 1. Crawl a site (stays on the seed domains)
python -m crawler crawl https://example.com --same-domain-only --max-pages 200

# 2. Search from the terminal
python -m crawler search example domain

# 3. Or launch the web UI at http://localhost:8000
python -m crawler serve
```

Prefer a config file? Copy `config.example.yaml` to `config.yaml`, edit it, then:

```bash
python -m crawler -c config.yaml crawl
```

## CLI

```
python -m crawler crawl [SEEDS...] [--max-pages N] [--max-depth N]
                        [--concurrency N] [--same-domain-only]
python -m crawler search QUERY... [-n LIMIT]
python -m crawler serve [--host 0.0.0.0] [--port 8000]
python -m crawler stats
```

Common options also come from env vars: `CRAWLER_SEEDS`, `CRAWLER_DATA_DIR`,
`CRAWLER_MAX_PAGES`, `CRAWLER_CONCURRENCY`, `CRAWLER_RESPECT_ROBOTS`, … (see
`crawler/config.py`).

## Running on unraid / Docker

The index lives in a single folder (`/data`), so persistence is just a volume.

```bash
# Build + start the search UI
docker compose up -d --build search        # serves on :8000

# Run a crawl into the same shared volume
docker compose run --rm crawl https://example.com --max-pages 500

# Search the populated index
docker compose run --rm crawl ../search "your query"   # or just use the UI
```

On **unraid**: add a container from this image, map a host path (e.g.
`/mnt/user/appdata/personalsearch`) to `/data`, and publish port `8000`.
Schedule recurring crawls with the *User Scripts* plugin calling
`docker compose run --rm crawl <seeds>` (or `docker exec`).

## Search syntax

Queries are tokenised and AND-ed, so `python async tutorial` finds documents
containing all three words. Ranking is BM25 with extra weight on titles, and
matches are highlighted in the snippet.

## Scope & politeness

- `same_domain_only` / `--same-domain-only` keeps the crawl on your seed hosts.
- `allowed_domains` / `blocked_domains` give precise control (subdomains match).
- `max_depth` and `max_pages` bound the crawl.
- `politeness_delay` throttles requests per host; robots.txt `Crawl-delay` is
  honoured and the per-host serialisation means you never hammer one server.

## Tests

```bash
pip install pytest
pytest -q          # network-free unit tests
```

## Notes & limitations

- JavaScript-rendered pages are not executed (HTML as served is indexed). A
  headless-browser fetcher could be added behind the `Fetcher` interface.
- The frontier is in-memory, so a crawl resumes fresh; already-indexed pages are
  simply re-upserted. Persisting the frontier is a natural next step.
```
