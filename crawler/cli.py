"""Command line interface: crawl, search, serve, stats."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .config import Config
from .crawler import WebCrawler
from .index import Index


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="crawler",
        description="A personal web crawler + full-text search index.",
    )
    p.add_argument("-c", "--config", help="Path to a YAML config file")
    p.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging"
    )
    sub = p.add_subparsers(dest="command", required=True)

    crawl = sub.add_parser("crawl", help="Crawl from seed URLs and build the index")
    crawl.add_argument("seeds", nargs="*", help="Seed URLs (override config)")
    crawl.add_argument("--max-pages", type=int)
    crawl.add_argument("--max-depth", type=int)
    crawl.add_argument("--concurrency", type=int)
    crawl.add_argument(
        "--same-domain-only",
        action="store_true",
        help="Only follow links within the seed domains",
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
    if getattr(args, "seeds", None):
        cfg.seeds = args.seeds
    return cfg


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = _load_config(args)

    if args.command == "crawl":
        if not cfg.seeds:
            print("No seeds given. Pass URLs or set them in the config.", file=sys.stderr)
            return 2
        summary = asyncio.run(WebCrawler(cfg).run())
        print(
            f"Done: {summary['pages_crawled']} pages indexed, "
            f"{summary['urls_seen']} URLs seen in {summary['elapsed_seconds']}s."
        )
        return 0

    if args.command == "search":
        index = Index(cfg.db_path)
        query = " ".join(args.query)
        hits = index.search(query, limit=args.limit)
        if not hits:
            print("No results.")
            return 0
        for i, hit in enumerate(hits, 1):
            snippet = hit.snippet.replace("<mark>", "\033[1m").replace("</mark>", "\033[0m")
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

        import os

        os.environ["CRAWLER_DATA_DIR"] = cfg.data_dir
        uvicorn.run("web.app:app", host=args.host, port=args.port, log_level="info")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
