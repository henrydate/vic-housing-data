"""
core.py — shared infrastructure for the vic_housing pipeline.

Provides:
  - SQLite initialisation & upsert helpers
  - Requests session with retries and a User-Agent
  - Disk-based HTTP response cache (default TTL 24 h)
  - Structured logger factory
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR   = Path(__file__).resolve().parent.parent
DB_PATH    = BASE_DIR / "vic_housing.db"
CACHE_DIR  = BASE_DIR / "cache"
LOG_DIR    = BASE_DIR / "logs"

CACHE_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

CACHE_TTL_SECONDS = int(os.getenv("VIC_CACHE_TTL", str(24 * 3600)))  # 24 h default


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def get_logger(name: str) -> logging.Logger:
    log = logging.getLogger(f"vic_housing.{name}")
    if not log.handlers:
        log.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                                datefmt="%Y-%m-%d %H:%M:%S")
        # Console
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        log.addHandler(ch)
        # File
        fh = logging.FileHandler(LOG_DIR / "pipeline.log")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    return log


# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------
def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "User-Agent": (
            "vic-housing-pipeline/1.0 "
            "(open-source research tool; github.com/yourname/vic-housing-data)"
        )
    })
    return session


# ---------------------------------------------------------------------------
# Disk cache
# ---------------------------------------------------------------------------
def _cache_path(url: str, params: dict | None = None) -> Path:
    key = url + json.dumps(params or {}, sort_keys=True)
    digest = hashlib.sha256(key.encode()).hexdigest()[:16]
    return CACHE_DIR / f"{digest}.json"


def cached_get(session: requests.Session, url: str,
               params: dict | None = None,
               ttl: int = CACHE_TTL_SECONDS) -> requests.Response | None:
    """
    Return cached response if fresh, otherwise fetch and cache.
    Returns the raw Response object (content as bytes via .content).
    """
    log = get_logger("cache")
    path = _cache_path(url, params)
    if path.exists():
        age = time.time() - path.stat().st_mtime
        if age < ttl:
            log.debug(f"Cache hit ({age:.0f}s old): {url}")
            cached = json.loads(path.read_text())
            # Reconstruct a minimal response-like object
            class _R:
                status_code = cached["status_code"]
                content     = cached["content"].encode("latin-1")
                text        = cached["content"]
                def raise_for_status(self): pass
                def json(self_): return json.loads(self_.text)
            return _R()

    log.debug(f"Cache miss: {url}")
    try:
        resp = session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        path.write_text(json.dumps({
            "status_code": resp.status_code,
            "content":     resp.content.decode("latin-1"),
        }))
        return resp
    except Exception as e:
        log.warning(f"Fetch failed ({url}): {e}")
        return None


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS sales_medians (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    period         TEXT    NOT NULL,   -- 'YYYY-QN' or 'YYYY'
    lga            TEXT,
    suburb         TEXT,
    dwelling_type  TEXT    NOT NULL,   -- 'house' | 'unit' | 'land'
    median_price   REAL,
    num_sales      INTEGER,
    source         TEXT    NOT NULL DEFAULT 'VGV',
    fetched_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(period, lga, suburb, dwelling_type)
);

CREATE TABLE IF NOT EXISTS rental_medians (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    period         TEXT    NOT NULL,   -- 'YYYY-QN'
    lga            TEXT,
    suburb         TEXT,
    dwelling_type  TEXT    NOT NULL,   -- '1br' | '2br' | '3br' | 'all'
    median_rent    REAL,               -- weekly $
    source         TEXT    NOT NULL DEFAULT 'DFFH',
    fetched_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(period, lga, suburb, dwelling_type)
);

CREATE TABLE IF NOT EXISTS building_approvals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    period         TEXT    NOT NULL,   -- 'YYYY-MM'
    region         TEXT    NOT NULL,
    dwelling_type  TEXT    NOT NULL,   -- 'house' | 'unit' | 'total'
    seasonality    TEXT    NOT NULL DEFAULT 'original',  -- 'original' | 'seasonally_adjusted'
    num_approvals  INTEGER,
    value_000      REAL,               -- $000
    source         TEXT    NOT NULL DEFAULT 'ABS',
    fetched_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(period, region, dwelling_type, seasonality)
);

CREATE TABLE IF NOT EXISTS lending_rates (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    period         TEXT    NOT NULL,   -- 'YYYY-MM'
    series_id      TEXT    NOT NULL,   -- e.g. 'FILRHLBVS'
    series_label   TEXT,
    rate_pct       REAL,
    source         TEXT    NOT NULL DEFAULT 'RBA',
    fetched_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(period, series_id)
);

CREATE TABLE IF NOT EXISTS asx_announcements (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker         TEXT    NOT NULL,
    announced_at   TEXT    NOT NULL,
    headline       TEXT    NOT NULL,
    url            TEXT,
    source         TEXT    NOT NULL DEFAULT 'ASX',
    fetched_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(ticker, announced_at, headline)
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    connector      TEXT    NOT NULL,
    status         TEXT    NOT NULL,   -- 'ok' | 'fail'
    new_rows       INTEGER DEFAULT 0,
    elapsed_s      REAL,
    error_msg      TEXT,
    run_at         TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    log = get_logger("core")
    with get_conn() as conn:
        conn.executescript(SCHEMA_SQL)
    log.info(f"Database ready: {DB_PATH}")


def upsert(table: str, rows: list[dict], conflict_cols: list[str]) -> int:
    """
    Generic upsert using INSERT OR IGNORE.
    Returns number of newly inserted rows.
    """
    if not rows:
        return 0
    cols        = list(rows[0].keys())
    placeholders = ", ".join(["?"] * len(cols))
    col_list     = ", ".join(cols)
    sql          = f"INSERT OR IGNORE INTO {table} ({col_list}) VALUES ({placeholders})"

    inserted = 0
    with get_conn() as conn:
        for row in rows:
            vals = [row[c] for c in cols]
            cur  = conn.execute(sql, vals)
            inserted += cur.rowcount
    return inserted
