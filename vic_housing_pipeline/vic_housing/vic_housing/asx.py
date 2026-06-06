"""
asx.py — ASX announcements connector.

Fetches recent ASX company announcements for a defined universe of
property-sector tickers via the ASX public JSON feed.

Tickers covered:
  MGR   Mirvac Group
  SGP   Stockland
  LLC   Lendlease
  DHG   Domain Holdings Australia
  GMG   Goodman Group
  REA   REA Group
  VCX   Vicinity Centres
  CQR   Charter Hall Retail REIT
  CLW   Charter Hall Long WALE REIT
  HMC   Home Consortium

Note: The ASX public feed provides recent announcements only (typically
last 20 per ticker). For a complete history you would need an ASX data
subscription or a service like Morningstar / Refinitiv.
"""

from __future__ import annotations

import time

from .core import build_session, cached_get, get_logger, upsert

log = get_logger("asx")

ASX_FEED_BASE = "https://www.asx.com.au/asx/1/company/{ticker}/announcements?count=20&market_sensitive=false"

TICKERS = [
    "MGR",  # Mirvac
    "SGP",  # Stockland
    "LLC",  # Lendlease
    "DHG",  # Domain Holdings
    "GMG",  # Goodman Group
    "REA",  # REA Group
    "VCX",  # Vicinity Centres
    "CQR",  # Charter Hall Retail REIT
    "CLW",  # Charter Hall Long WALE REIT
    "HMC",  # Home Consortium
]

RATE_LIMIT_SECS = 1.0  # be polite to ASX servers


def _parse_announcements(data: dict, ticker: str) -> list[dict]:
    rows = []
    announcements = data.get("data", [])
    for ann in announcements:
        date   = ann.get("date", "") or ann.get("published_date", "")
        title  = ann.get("header", "") or ann.get("title", "") or ann.get("headline", "")
        url    = ann.get("url", "") or ann.get("document_release_url", "")

        if not title:
            continue

        # Normalise date to ISO format
        if "T" in str(date):
            date = str(date).split("T")[0]

        rows.append({
            "ticker":       ticker,
            "announced_at": str(date),
            "headline":     str(title).strip(),
            "url":          str(url).strip() if url else None,
        })
    return rows


def run() -> int:
    session  = build_session()
    all_rows = []

    for ticker in TICKERS:
        url = ASX_FEED_BASE.format(ticker=ticker)
        log.info(f"Fetching ASX announcements: {ticker}")
        resp = cached_get(session, url, ttl=3600)  # 1-hour cache for live feed
        if resp is None:
            log.warning(f"  {ticker}: fetch failed")
            time.sleep(RATE_LIMIT_SECS)
            continue

        try:
            data = resp.json()
        except Exception as e:
            log.warning(f"  {ticker}: JSON parse error: {e}")
            time.sleep(RATE_LIMIT_SECS)
            continue

        rows = _parse_announcements(data, ticker)
        log.info(f"  {ticker}: {len(rows)} announcements")
        all_rows.extend(rows)
        time.sleep(RATE_LIMIT_SECS)

    new = upsert("asx_announcements", all_rows, ["ticker", "announced_at", "headline"])
    log.info(f"ASX: {len(all_rows)} rows parsed → {new} new inserted")
    return new
