"""
asx.py -- ASX announcements connector (MarkitDigital backend).

asx.com.au/asx/1/... endpoints are Akamai-blocked since Feb 2024.

Working endpoint (confirmed June 2026):
  asx.api.markitdigital.com/asx-research/1.0/companies/{ticker}/announcements
  JSON structure: {"data": {"displayName": "...", "items": [{...}, ...]}}

Fallback:
  EODHD -- set EODHD_API_KEY env var for stable, paid coverage.
"""

from __future__ import annotations

import os
import time

from .core import build_session, get_logger, upsert

log = get_logger("asx")

MARKIT_URL    = (
    "https://asx.api.markitdigital.com/asx-research/1.0/companies"
    "/{ticker}/announcements?count=20"
)
EODHD_ANN_URL = (
    "https://eodhd.com/api/news?s={ticker}.AU"
    "&api_token={key}&limit=20&fmt=json"
)

TICKERS = [
    "MGR",   # Mirvac Group
    "SGP",   # Stockland
    "LLC",   # Lendlease
    # DHG removed -- Domain Holdings delisted after REA acquisition
    "GMG",   # Goodman Group
    "REA",   # REA Group
    "VCX",   # Vicinity Centres
    "CQR",   # Charter Hall Retail REIT
    "CLW",   # Charter Hall Long WALE REIT
    "HMC",   # Home Consortium
    "APD",   # Apiam Animal Health (replaced DHG)
    "DXS",   # Dexus (office/industrial REIT)
]

RATE_LIMIT_SECS = 1.2


def _parse_markit(data: dict, ticker: str) -> list[dict]:
    """
    Confirmed MarkitDigital JSON structure (June 2026):
      {
        "data": {
          "displayName": "MIRVAC GROUP",
          "issueType": "UT",
          "items": [
            {
              "announcementType": "...",
              "date": "2026-06-01T23:22:21.000Z",
              "documentKey": "...",
              "fileSize": "...",
              "headline": "...",
              "isPriceSensitive": false,
              "url": "..."
            },
            ...
          ]
        }
      }
    """
    rows = []
    # Primary path: data.items
    items = (
        data.get("data", {}).get("items", [])
        or data.get("data", {}).get("announcements", [])  # older format fallback
        or data.get("items", [])
        or data.get("announcements", [])
    )
    if isinstance(items, dict):
        items = list(items.values())

    for ann in items:
        if not isinstance(ann, dict):
            continue
        date  = (ann.get("date") or ann.get("displayIssueDate")
                 or ann.get("issueDate") or "")
        title = (ann.get("headline") or ann.get("header")
                 or ann.get("title") or "").strip()
        url   = (ann.get("url") or ann.get("documentUrl") or "").strip()

        if not title:
            continue

        # Normalise date to YYYY-MM-DD
        if "T" in str(date):
            date = str(date).split("T")[0]
        date = str(date)[:10]

        rows.append({
            "ticker":       ticker,
            "announced_at": date,
            "headline":     title,
            "url":          url or None,
        })
    return rows


def _fetch_markit(session, ticker: str) -> list[dict]:
    url  = MARKIT_URL.format(ticker=ticker)
    try:
        r = session.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
        rows = _parse_markit(data, ticker)
        log.info(f"  {ticker}: {len(rows)} announcements (MarkitDigital)")
        return rows
    except Exception as e:
        log.warning(f"  {ticker}: MarkitDigital error: {e}")
        return []


def _fetch_eodhd(session, ticker: str, api_key: str) -> list[dict]:
    url = EODHD_ANN_URL.format(ticker=ticker, key=api_key)
    try:
        r = session.get(url, timeout=15)
        r.raise_for_status()
        items = r.json()
        rows  = []
        for item in (items if isinstance(items, list) else []):
            rows.append({
                "ticker":       ticker,
                "announced_at": str(item.get("date", ""))[:10],
                "headline":     (item.get("title") or "").strip(),
                "url":          item.get("link") or None,
            })
        log.info(f"  {ticker}: {len(rows)} announcements (EODHD)")
        return rows
    except Exception as e:
        log.warning(f"  {ticker}: EODHD error: {e}")
        return []


def run() -> int:
    session   = build_session()
    all_rows  = []
    eodhd_key = os.getenv("EODHD_API_KEY", "")

    for ticker in TICKERS:
        rows = _fetch_markit(session, ticker)

        if not rows and eodhd_key:
            log.info(f"  {ticker}: MarkitDigital empty, trying EODHD")
            rows = _fetch_eodhd(session, ticker, eodhd_key)

        if not rows:
            log.warning(
                f"  {ticker}: no announcements fetched. "
                "Set EODHD_API_KEY env var for a stable fallback."
            )

        all_rows.extend(rows)
        time.sleep(RATE_LIMIT_SECS)

    new = upsert("asx_announcements", all_rows,
                 ["ticker", "announced_at", "headline"])
    log.info(f"ASX: {len(all_rows)} rows -> {new} new inserted")
    return new
