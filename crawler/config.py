"""Crawler configuration: dataclass with YAML + environment overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - yaml is a declared dependency
    yaml = None


# Content types we know how to extract searchable text from. Everything else is
# still indexed by metadata (URL, type, size) when ``index_all_types`` is on.
DEFAULT_TEXTUAL_TYPES = [
    "text/html",
    "application/xhtml+xml",
    "text/plain",
    "text/markdown",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
]


@dataclass
class Config:
    # Where to start.
    seeds: list[str] = field(default_factory=list)

    # Storage.
    data_dir: str = "./data"

    # Identity / politeness.
    user_agent: str = (
        "PersonalCrawler/0.1 (+https://github.com/; personal indexing bot)"
    )
    # robots.txt is off by default for this personal crawler. Politeness still
    # applies; be considerate (and mind site Terms of Service).
    respect_robots: bool = False
    politeness_delay: float = 1.0  # seconds between hits to the same host

    # How often (seconds) to print a progress heartbeat while crawling.
    progress_interval: float = 5.0

    # Safety. Block fetches that resolve to private/loopback/link-local space so
    # the crawler can't be steered into your LAN or cloud metadata endpoints.
    block_private_addresses: bool = True

    # JavaScript rendering (optional, requires Playwright + browsers installed).
    render_js: bool = False
    render_wait_ms: int = 1500  # extra settle time after page load

    # Real-browser mode: fetch pages with a VISIBLE, persistent Chromium so you
    # can solve a bot challenge (e.g. Cloudflare) once and reuse the clearance.
    # Slow and needs a desktop session; meant for a few tough sites.
    real_browser: bool = False
    browser_profile_dir: str = ""        # default: <data_dir>/browser-profile
    browser_solve_timeout: int = 180     # seconds to wait for you to solve a challenge

    # Resumability / re-crawling.
    resume: bool = True  # persist the frontier and resume pending URLs
    recrawl_after_days: float = 0.0  # >0 re-queues docs older than this on crawl

    # Concurrency / limits.
    concurrency: int = 10
    max_pages: int = 10_000
    max_depth: int = 5
    request_timeout: int = 20
    max_content_bytes: int = 10 * 1024 * 1024  # 10 MiB

    # Scope control.
    same_domain_only: bool = False
    allowed_domains: list[str] = field(default_factory=list)  # empty => any
    blocked_domains: list[str] = field(default_factory=list)

    # Skip any URL containing one of these substrings (e.g. "/logout", "?sort=").
    exclude_patterns: list[str] = field(default_factory=list)

    # Skip indexing a page whose exact text already exists under another URL.
    deduplicate: bool = True

    # What to keep.
    index_all_types: bool = True  # store metadata even for binaries
    textual_content_types: list[str] = field(
        default_factory=lambda: list(DEFAULT_TEXTUAL_TYPES)
    )

    @property
    def db_path(self) -> str:
        return str(Path(self.data_dir) / "index.db")

    # ------------------------------------------------------------------ #
    # Loading
    # ------------------------------------------------------------------ #
    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "Config":
        """Build config from optional YAML file then environment overrides."""
        data: dict = {}
        if path:
            p = Path(path)
            if p.exists():
                if yaml is None:
                    raise RuntimeError("PyYAML is required to read config files")
                data = yaml.safe_load(p.read_text()) or {}

        cfg = cls(**{k: v for k, v in data.items() if k in _field_names()})
        cfg._apply_env()
        Path(cfg.data_dir).mkdir(parents=True, exist_ok=True)
        return cfg

    def _apply_env(self) -> None:
        """Override fields from CRAWLER_* environment variables."""
        mapping = {
            "CRAWLER_DATA_DIR": ("data_dir", str),
            "CRAWLER_USER_AGENT": ("user_agent", str),
            "CRAWLER_CONCURRENCY": ("concurrency", int),
            "CRAWLER_MAX_PAGES": ("max_pages", int),
            "CRAWLER_MAX_DEPTH": ("max_depth", int),
            "CRAWLER_POLITENESS_DELAY": ("politeness_delay", float),
            "CRAWLER_REQUEST_TIMEOUT": ("request_timeout", int),
            "CRAWLER_RESPECT_ROBOTS": ("respect_robots", _as_bool),
            "CRAWLER_SAME_DOMAIN_ONLY": ("same_domain_only", _as_bool),
            "CRAWLER_BLOCK_PRIVATE": ("block_private_addresses", _as_bool),
            "CRAWLER_RENDER_JS": ("render_js", _as_bool),
            "CRAWLER_RESUME": ("resume", _as_bool),
            "CRAWLER_RECRAWL_AFTER_DAYS": ("recrawl_after_days", float),
        }
        for env, (attr, caster) in mapping.items():
            raw = os.environ.get(env)
            if raw is not None and raw != "":
                setattr(self, attr, caster(raw))

        seeds = os.environ.get("CRAWLER_SEEDS")
        if seeds:
            self.seeds = [s.strip() for s in seeds.split(",") if s.strip()]


def _field_names() -> set[str]:
    return {f.name for f in fields(Config)}


def _as_bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")
