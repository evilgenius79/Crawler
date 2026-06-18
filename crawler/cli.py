"""Command line interface: crawl, recrawl, schedule, search, serve, stats."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time

from .config import Config
from .crawler import WebCrawler
from .index import HL_CLOSE, HL_OPEN, Index

log = logging.getLogger("crawler.cli")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="crawler",
        description="A personal web crawler + full-text search index.",
    )
    p.add_argument("-c", "--config", help="Path to a YAML config file")
    p.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    sub = p.add_subparsers(dest="command", required=True)

    def add_crawl_flags(sp):
        sp.add_argument("seeds", nargs="*", help="Seed URLs (override config)")
        sp.add_argument("--max-pages", type=int)
        sp.add_argument("--max-depth", type=int)
        sp.add_argument("--concurrency", type=int)
        sp.add_argument(
            "--same-domain-only",
            action="store_true",
            help="Only follow links within the seed domains",
        )
        sp.add_argument(
            "--no-sitemaps",
            action="store_true",
            help="Don't seed the frontier from robots.txt / sitemap.xml",
        )
        sp.add_argument(
            "--ignore-robots",
            action="store_true",
            help="Do not fetch or obey robots.txt (this is the default)",
        )
        sp.add_argument(
            "--respect-robots",
            action="store_true",
            help="Fetch and obey robots.txt rules",
        )
        sp.add_argument(
            "--render-js",
            action="store_true",
            help="Render pages with a headless browser (needs Playwright)",
        )
        sp.add_argument(
            "--real-browser",
            action="store_true",
            help="Fetch via a visible, persistent browser so you can solve bot "
            "challenges (needs Playwright + a desktop session; slow)",
        )
        sp.add_argument(
            "--no-resume",
            action="store_true",
            help="Ignore the persisted frontier; start fresh",
        )
        sp.add_argument(
            "--allow-private",
            action="store_true",
            help="Permit crawling private/loopback addresses (unsafe)",
        )
        sp.add_argument(
            "--user-agent",
            help="Override the User-Agent header sent on every request",
        )

    crawl = sub.add_parser("crawl", help="Crawl from seed URLs and build the index")
    add_crawl_flags(crawl)

    recrawl = sub.add_parser(
        "recrawl", help="Re-crawl documents older than N days (incremental update)"
    )
    add_crawl_flags(recrawl)
    recrawl.add_argument(
        "--older-than-days",
        type=float,
        default=7.0,
        help="Re-crawl documents last fetched more than this many days ago",
    )

    schedule = sub.add_parser(
        "schedule", help="Run a recurring crawl/recrawl on an interval"
    )
    add_crawl_flags(schedule)
    schedule.add_argument(
        "--interval",
        type=float,
        default=3600.0,
        help="Seconds between runs (default: 3600)",
    )
    schedule.add_argument(
        "--older-than-days",
        type=float,
        default=1.0,
        help="On each run, re-crawl documents older than this many days",
    )

    search = sub.add_parser("search", help="Search the index from the terminal")
    search.add_argument("query", nargs="+", help="Search terms")
    search.add_argument("-n", "--limit", type=int, default=10)

    serve = sub.add_parser("serve", help="Launch the web search UI")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)

    sub.add_parser("stats", help="Show index statistics")
    return p


def _load_config(args) -> Config:
    cfg = Config.load(getattr(args, "config", None))
    for attr in ("max_pages", "max_depth", "concurrency"):
        val = getattr(args, attr, None)
        if val is not None:
            setattr(cfg, attr, val)
    if getattr(args, "same_domain_only", False):
        cfg.same_domain_only = True
    if getattr(args, "ignore_robots", False):
        cfg.respect_robots = False
    if getattr(args, "respect_robots", False):
        cfg.respect_robots = True
    if getattr(args, "render_js", False):
        cfg.render_js = True
    if getattr(args, "real_browser", False):
        cfg.real_browser = True
    if getattr(args, "no_resume", False):
        cfg.resume = False
    if getattr(args, "allow_private", False):
        cfg.block_private_addresses = False
    if getattr(args, "older_than_days", None) is not None:
        cfg.recrawl_after_days = args.older_than_days
    if getattr(args, "user_agent", None):
        cfg.user_agent = args.user_agent
    if getattr(args, "no_sitemaps", False):
        cfg.use_sitemaps = False
    if getattr(args, "seeds", None):
        cfg.seeds = args.seeds
    return cfg


def _run_crawl(cfg: Config) -> int:
    # A run needs *something* to do: seeds, a re-crawl window, or pending URLs
    # left over from a previous resumable crawl. Reuse one Index for the check
    # and the crawl so we don't open/bootstrap the DB twice per run.
    index = Index(cfg.db_path)
    has_pending = cfg.resume and bool(index.frontier_pending())
    if not cfg.seeds and cfg.recrawl_after_days <= 0 and not has_pending:
        index.close()
        print("No seeds given. Pass URLs or set them in the config.", file=sys.stderr)
        return 2
    try:
        summary = asyncio.run(WebCrawler(cfg, index=index).run())
    finally:
        index.close()
    print(
        f"Done: {summary['pages_crawled']} pages indexed, "
        f"{summary.get('errors', 0)} errors, "
        f"{summary['urls_seen']} URLs seen in {summary['elapsed_seconds']}s."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = _load_config(args)

    if args.command in ("crawl", "recrawl"):
        return _run_crawl(cfg)

    if args.command == "schedule":
        if not cfg.seeds:
            print("Scheduling needs seed URLs.", file=sys.stderr)
            return 2
        log.info("Scheduler started: every %.0fs", args.interval)
        try:
            while True:
                _run_crawl(cfg)
                log.info("Sleeping %.0fs until next run", args.interval)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nScheduler stopped.")
            return 0

    if args.command == "search":
        index = Index(cfg.db_path)
        query = " ".join(args.query)
        hits = index.search(query, limit=args.limit)
        if not hits:
            print("No results.")
            return 0
        for i, hit in enumerate(hits, 1):
            snippet = hit.snippet.replace(HL_OPEN, "\033[1m").replace(HL_CLOSE, "\033[0m")
            print(f"{i}. {hit.title}\n   {hit.url}\n   {snippet}\n")
        return 0

    if args.command == "stats":
        index = Index(cfg.db_path)
        stats = index.stats()
        print(f"Documents indexed: {stats['total_documents']}")
        print(f"Database: {cfg.db_path}")
        for ctype, n in stats["by_content_type"].items():
            print(f"  {ctype or '(unknown)'}: {n}")
        return 0

    if args.command == "serve":
        import uvicorn

        os.environ["CRAWLER_DATA_DIR"] = cfg.data_dir
        uvicorn.run("web.app:app", host=args.host, port=args.port, log_level="info")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
