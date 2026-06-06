"""
rba.py — RBA Statistical Tables connector.

Sources (CSV):
  F5  — Indicator Lending Rates (owner-occupier & investor, P&I and IO)
  F6  — Housing Loan Commitments
  F7  — Variable Housing Lending Rates

These are the series most relevant to rent-vs-buy modelling and property
research. All are publicly available and update monthly.
"""

from __future__ import annotations

import csv
import io
import re

from .core import build_session, cached_get, get_logger, upsert

log = get_logger("rba")

RBA_BASE = "https://www.rba.gov.au/statistics/tables"

SERIES = {
    "F5": {
        "url":   f"{RBA_BASE}/f5.1-data.csv",
        "label": "Indicator Lending Rates",
    },
    "F6": {
        "url":   f"{RBA_BASE}/f6-data.csv",
        "label": "Housing Loan Commitments",
    },
    "F7": {
        "url":   f"{RBA_BASE}/f7-data.csv",
        "label": "Variable Housing Lending Rates",
    },
}

# Only persist series whose IDs contain these substrings
SERIES_FILTER = [
    "FILRHLB",   # Housing lending rate benchmarks
    "FILRHLO",   # Owner-occupier
    "FILRHLI",   # Investor
    "FILRHLP",   # P&I
    "FILRHLL",   # IO
    "HHCLOAN",   # Housing loan commitments
    "SVRATE",    # Standard variable rate
]


def _matches_filter(series_id: str) -> bool:
    sid = series_id.upper()
    return any(f in sid for f in SERIES_FILTER) or True  # keep all by default


def _parse_rba_csv(text: str, table_id: str) -> list[dict]:
    """
    RBA CSVs have a metadata header block followed by data rows.
    Format:
        Row 1:  Series ID    | ID1  | ID2  | ...
        Row 2:  Description  | desc | desc | ...
        Row 3:  Frequency    | ...
        Row 4:  Type         | ...
        Row 5:  Units        | ...
        Row 6+: data         | YYYY-Mon | val | val | ...
    """
    rows_out = []
    reader   = csv.reader(io.StringIO(text))
    lines    = list(reader)

    if len(lines) < 6:
        log.warning(f"  {table_id}: too few rows in CSV ({len(lines)})")
        return []

    # Row 0: series IDs
    series_ids = lines[0]
    # Row 1: descriptions
    descriptions = lines[1] if len(lines) > 1 else [""] * len(series_ids)

    # Data starts after the metadata block — find the first row where col[0] looks like a date
    data_start = None
    for i, row in enumerate(lines):
        if row and re.match(r"\d{4}", row[0].strip()):
            data_start = i
            break

    if data_start is None:
        log.warning(f"  {table_id}: could not find data rows")
        return []

    for row in lines[data_start:]:
        if not row or not row[0].strip():
            continue
        # Parse period: RBA uses "YYYY-Mon" or "YYYY-MM"
        raw_period = row[0].strip()
        m = re.match(r"(\d{4})-([A-Za-z]{3})", raw_period)
        if m:
            month_map = {
                "Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04",
                "May": "05", "Jun": "06", "Jul": "07", "Aug": "08",
                "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12",
            }
            period = f"{m.group(1)}-{month_map.get(m.group(2), '01')}"
        else:
            m2 = re.match(r"(\d{4})-(\d{2})", raw_period)
            period = f"{m2.group(1)}-{m2.group(2)}" if m2 else raw_period

        for col_idx in range(1, len(row)):
            if col_idx >= len(series_ids):
                break
            series_id = series_ids[col_idx].strip()
            if not series_id:
                continue
            label = descriptions[col_idx].strip() if col_idx < len(descriptions) else ""
            val   = row[col_idx].strip()
            if not val or val in ("", "..", "na", "NA", "N/A"):
                continue
            try:
                rate = float(val)
            except ValueError:
                continue

            rows_out.append({
                "period":       period,
                "series_id":    series_id,
                "series_label": label or f"{table_id}/{series_id}",
                "rate_pct":     rate,
            })

    return rows_out


def run() -> int:
    session  = build_session()
    all_rows = []

    for table_id, meta in SERIES.items():
        log.info(f"Fetching RBA {table_id}: {meta['label']}")
        resp = cached_get(session, meta["url"])
        if resp is None:
            log.warning(f"  {table_id}: fetch failed")
            continue

        rows = _parse_rba_csv(resp.text, table_id)
        log.info(f"  {table_id}: {len(rows)} observations parsed")
        all_rows.extend(rows)

    new = upsert("lending_rates", all_rows, ["period", "series_id"])
    log.info(f"RBA: {len(all_rows)} rows parsed → {new} new inserted")
    return new
