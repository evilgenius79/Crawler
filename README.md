# PersonalSearch — a personal web crawler & search engine

A small, fast, self-hosted crawler and full-text search index. Point it at some
seed URLs, let it crawl, then search your own corner of the web from a clean,
ad-free, Google-style UI. Designed to run happily on an unraid box (or any
Docker host).

- **Async crawler** (aiohttp) — many pages in flight at once, polite per host
- **Crawls everything** — HTML, PDF, DOCX and plain text get full-text indexed;
  every other file type is still indexed by URL/type/size so nothing is lost
- **Optional JavaScript rendering** via a headless browser (Playwright)
- **Resumable** — the frontier is persisted, so an interrupted crawl picks up
  where it left off
- **Incremental & scheduled re-crawling** — keep the index fresh automatically
- **SSRF-hardened** — refuses to crawl private/loopback/cloud-metadata addresses
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
| `crawler/frontier.py`   | De-duplicated BFS queue of URLs (resumable) |
| `crawler/fetcher.py`    | Async HTTP with timeout, size cap, SSRF-safe resolver |
| `crawler/render.py`     | Optional headless-browser rendering (Playwright) |
| `crawler/robots.py`     | Cached robots.txt rules & crawl-delay |
| `crawler/extractors.py` | HTML/PDF/DOCX/text → title + text + links |
| `crawler/index.py`      | SQLite FTS5 store, upsert + ranked search + frontier |
| `crawler/security.py`   | Private/reserved address checks (SSRF protection) |
| `crawler/crawler.py`    | Orchestrates workers, scope, politeness, persistence |
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
python -m crawler crawl    [SEEDS...] [--max-pages N] [--max-depth N]
                           [--concurrency N] [--same-domain-only]
                           [--render-js] [--no-resume] [--allow-private]
python -m crawler recrawl  [SEEDS...] [--older-than-days N]   # incremental update
python -m crawler schedule [SEEDS...] [--interval SECONDS] [--older-than-days N]
python -m crawler search   QUERY... [-n LIMIT]
python -m crawler serve    [--host 0.0.0.0] [--port 8000]
python -m crawler stats
```

Common options also come from env vars: `CRAWLER_SEEDS`, `CRAWLER_DATA_DIR`,
`CRAWLER_MAX_PAGES`, `CRAWLER_CONCURRENCY`, `CRAWLER_RESPECT_ROBOTS`,
`CRAWLER_BLOCK_PRIVATE`, `CRAWLER_RENDER_JS`, `CRAWLER_RESUME` … (see
`crawler/config.py`).

### Resumable crawls

The frontier is persisted in the index database, so if a crawl is interrupted
(Ctrl-C, container restart) just run the same `crawl` again — it resumes the
pending URLs instead of starting over. Use `--no-resume` to force a fresh start.

### Incremental & scheduled re-crawling

```bash
# Re-fetch anything indexed more than 7 days ago, then keep crawling
python -m crawler recrawl --older-than-days 7

# Run forever: crawl seeds + re-crawl day-old pages every hour
python -m crawler schedule https://example.com --interval 3600 --older-than-days 1
```

On unraid, the **User Scripts** plugin (cron) calling `recrawl` is usually
nicer than a long-running `schedule` process — see below.

### JavaScript rendering

Some sites render content with JavaScript. Enable a headless browser to index
the rendered DOM:

```bash
pip install playwright && playwright install chromium
python -m crawler crawl https://spa.example.com --render-js
```

If Playwright (or its browsers) isn't installed, the crawler logs a warning and
falls back to plain HTTP fetching, so nothing breaks.

## Running on unraid / Docker

The index lives in a single folder (`/data`), so persistence is just a volume.

```bash
# Build + start the search UI
docker compose up -d --build search        # serves on :8000

# Run a crawl into the same shared volume
docker compose run --rm crawl https://example.com --max-pages 500
```

**Community Apps template:** `unraid-template.xml` is included. Drop it in
`/boot/config/plugins/dockerMan/templates-user/` (or point Add Container at its
URL), map a host path such as `/mnt/user/appdata/personalsearch` to `/data`,
publish port `8000`, and start. Schedule recurring updates with the *User
Scripts* plugin:

```bash
docker exec personal-search python -m crawler recrawl --older-than-days 1
```

## Search syntax

Queries are tokenised and AND-ed, so `python async tutorial` finds documents
containing all three words. Ranking is BM25 with extra weight on titles, and
matches are highlighted in the snippet.

## Security

This crawler follows links from untrusted pages, so it is built to be hard to
abuse:

- **SSRF protection (on by default).** Hosts that resolve to private, loopback,
  link-local, reserved or multicast addresses are refused. For the HTTP fetch
  path, filtering happens at **connect time** via a custom DNS resolver, so it
  also covers HTTP redirects and DNS-rebinding — not just the initial URL.
  When `--render-js` is enabled, the headless browser does its own networking;
  requests to private addresses are blocked on a **best-effort** basis (a
  rebinding race inside the browser's stack can't be fully closed), so only
  enable rendering for sources you trust. Disable the checks entirely only on a
  trusted LAN with `--allow-private` / `block_private_addresses: false`.
- **No stored XSS in the UI.** Crawled page text is HTML-escaped before display;
  search highlights use internal sentinels that are converted to `<mark>` only
  after escaping, so page markup can never inject script into the results page.
- **No SQL/FTS injection.** All queries are parameterised and user search input
  is tokenised into a safe FTS5 expression.
- **Bounded resources.** Responses are capped at `max_content_bytes` (default
  10 MiB) while streaming, so a huge or decompression-bomb response can't
  exhaust memory.

The web UI has no authentication and binds `0.0.0.0` by default — keep it on a
trusted network or behind a reverse proxy / VPN. The JSON API's `snippet` field
is HTML-escaped (only the `<mark>` highlights are markup), so it is safe to drop
into the DOM; the `title` field is the raw page title and should be treated as
text.

## Tests

```bash
pip install pytest
pytest -q          # network-free unit tests
```

## Notes & limitations

- JavaScript is only executed when `--render-js` is enabled (and Playwright is
  installed); otherwise the HTML as served is indexed.
- The de-dup "seen" set is kept in memory during a run (the frontier itself is
  persisted), so very large crawls are bounded by `max_pages`/RAM.
- `max_pages` is a *soft* limit: with `concurrency` workers in flight, a crawl
  can overshoot it by up to roughly `concurrency` pages before stopping.
