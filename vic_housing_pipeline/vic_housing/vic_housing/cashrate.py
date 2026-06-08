"""
cashrate.py -- RBA Cash Rate Target connector.

Source: RBA statistical table F1.1 (Interest Rates and Yields - Money Market),
        free CSV, no auth. Series FIRMMCRT = Cash Rate Target (monthly average).

Cadence: monthly, 1990-present. Powers the rate-sensitivity analysis (the cash
rate is the policy instrument; lending rates in F5/F6 are downstream of it).
"""
from __future__ import annotations

import csv
import io

import pandas as pd

from .core import build_session, cached_get, get_logger, upsert

log = get_logger("cashrate")

F11_URL = "https://www.rba.gov.au/statistics/tables/csv/f1.1-data.csv"
CASH_RATE_SERIES = "FIRMMCRT"   # Cash Rate Target, monthly average


def run() -> int:
    session = build_session()
    log.info("Fetching RBA Cash Rate Target (F1.1)")
    resp = cached_get(session, F11_URL)
    if resp is None:
        log.warning("  F1.1: fetch failed")
        return 0

    rows_csv = list(csv.reader(io.StringIO(resp.text)))

    # Locate the 'Series ID' row and the target column
    sid_row = next((i for i, r in enumerate(rows_csv)
                    if r and r[0].strip() == "Series ID"), None)
    if sid_row is None:
        log.warning("  F1.1: 'Series ID' row not found")
        return 0
    try:
        col = rows_csv[sid_row].index(CASH_RATE_SERIES)
    except ValueError:
        log.warning(f"  F1.1: series {CASH_RATE_SERIES} not present")
        return 0

    out = []
    for r in rows_csv[sid_row + 1:]:
        if not r or not r[0].strip():
            continue
        try:
            dt = pd.to_datetime(r[0].strip(), dayfirst=True)
            val = float(r[col])
        except (ValueError, IndexError):
            continue
        out.append({"period": dt.strftime("%Y-%m"), "rate_pct": val})

    new = upsert("cash_rate", out, ["period"])
    log.info(f"Cash rate: {len(out)} months parsed -> {new} new inserted")
    return new
