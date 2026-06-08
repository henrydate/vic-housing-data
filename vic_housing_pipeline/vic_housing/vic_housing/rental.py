"""
rental.py -- DFFH Rental Report connector.

Source: dffh.vic.gov.au publications page (live scrape -- no hardcoded URLs)

DFFH Excel file structure (confirmed Sep 2025):
  Sheets: '1 bedroom flat', '2 bedroom flat', '3 bedroom flat',
          '2 bedroom house', '3 bedroom house', '4 bedroom house',
          'All properties'

  Row 0:  Title row (skip)
  Row 1:  Quarter headers starting col 2 -- e.g. 'Mar 2000', 'Jun 2000', ...
          Each quarter uses TWO columns (Count, Median), repeated ~104 times
  Row 2:  'Count' / 'Median' alternating labels
  Row 3+: Data rows
          Col 0 = LGA name (only in first row of LGA group -- forward-fill)
          Col 1 = Suburb / town name
          Cols 2+ = Count, Median pairs for each quarter

Key insight: each quarterly file is CUMULATIVE -- it contains ALL quarters
from 2000 to the current quarter. We only need to download ONE file to get
the full history.
"""

from __future__ import annotations

import re
from io import BytesIO

import pandas as pd
from bs4 import BeautifulSoup

from .core import build_session, get_logger, upsert

log = get_logger("rental")

DFFH_BASE         = "https://www.dffh.vic.gov.au"
DFFH_CURRENT_PAGE = f"{DFFH_BASE}/publications/rental-report"
DFFH_PAST_PAGE    = f"{DFFH_BASE}/publications/past-rental-reports"

SHEET_TO_DWELLING = {
    "1 bedroom flat":   "1br",
    "2 bedroom flat":   "2br",
    "3 bedroom flat":   "3br",
    "2 bedroom house":  "2br_house",
    "3 bedroom house":  "3br_house",
    "4 bedroom house":  "4br",
    "all properties":   "all",
}

MONTH_TO_Q = {"mar": "Q1", "jun": "Q2", "sep": "Q3", "dec": "Q4"}


def _date_to_period(s: str) -> str | None:
    """Convert 'Mar 2000' -> '2000-Q1', etc."""
    m = re.match(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+(\d{4})",
                 str(s).lower())
    if m:
        # Map any month to the nearest quarter end
        month = m.group(1)[:3]
        q_map = {
            "jan": "Q1", "feb": "Q1", "mar": "Q1",
            "apr": "Q2", "may": "Q2", "jun": "Q2",
            "jul": "Q3", "aug": "Q3", "sep": "Q3",
            "oct": "Q4", "nov": "Q4", "dec": "Q4",
        }
        return f"{m.group(2)}-{q_map.get(month, 'Q?')}"
    return None


def _get_latest_suburb_url(session) -> str | None:
    """
    Scrape DFFH publications pages to find the most recent
    'moving-annual-rent*suburb*' Excel file URL.
    Downloads that one file which contains ALL quarters (2000-present).
    """
    for page_url in (DFFH_CURRENT_PAGE, DFFH_PAST_PAGE):
        try:
            r = session.get(page_url, timeout=15)
            r.raise_for_status()
            soup = BeautifulSoup(r.content, "lxml")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "moving-annual-rent" in href.lower() and "suburb" in href.lower():
                    full = href if href.startswith("http") else f"{DFFH_BASE}{href}"
                    log.info(f"  Latest suburb file link: {full}")
                    return full
        except Exception as e:
            log.warning(f"  Could not scrape {page_url}: {e}")
    return None


def _parse_dffh_sheet(df: pd.DataFrame, dwelling_type: str) -> list[dict]:
    """
    Parse one sheet of the DFFH cumulative wide-format Excel file.
    Extracts ALL quarters (median rent per suburb per quarter).
    """
    if df.shape[0] < 4 or df.shape[1] < 4:
        return []

    # Row 1 = quarter date labels (starting col 2)
    # Row 2 = 'Count' / 'Median' alternating
    date_row = df.iloc[1]
    cm_row   = df.iloc[2]

    # Map column index -> (period_str, is_median)
    median_cols: dict[int, str] = {}
    current_period = None
    for c in range(2, df.shape[1]):
        dv = date_row.iloc[c]
        cv = str(cm_row.iloc[c]).lower().strip() if pd.notna(cm_row.iloc[c]) else ""

        if pd.notna(dv):
            current_period = _date_to_period(str(dv))

        if current_period and "median" in cv:
            median_cols[c] = current_period

    if not median_cols:
        return []

    rows = []
    current_lga = None

    for i in range(3, len(df)):
        row = df.iloc[i]

        # Forward-fill LGA (col 0 only populated for first suburb in each LGA)
        lga_val = row.iloc[0]
        if pd.notna(lga_val) and str(lga_val).strip() not in ("", "nan"):
            current_lga = str(lga_val).strip()

        suburb_val = row.iloc[1]
        if pd.isna(suburb_val):
            continue
        suburb = str(suburb_val).strip()
        if not suburb or suburb.lower() in ("nan", "none", "suburb", ""):
            continue

        for col_idx, period in median_cols.items():
            val = row.iloc[col_idx]
            if pd.isna(val):
                continue
            try:
                rent = float(str(val).replace(",", "").strip())
            except (ValueError, TypeError):
                continue
            if rent <= 0:
                continue

            rows.append({
                "period":        period,
                "lga":           current_lga,
                "suburb":        suburb,
                "dwelling_type": dwelling_type,
                "median_rent":   rent,
            })

    return rows


def run() -> int:
    session  = build_session()
    all_rows = []

    # --- Step 1: find latest suburb Excel URL ---------------------------------
    log.info("Locating latest DFFH rental Excel file...")
    page_url = _get_latest_suburb_url(session)
    if not page_url:
        log.warning("  Could not find DFFH suburb rental URL")
        return 0

    # --- Step 2: download (the page redirects directly to .xlsx) -------------
    log.info(f"  Downloading: {page_url}")
    try:
        r = session.get(page_url, timeout=60, allow_redirects=True)
        r.raise_for_status()
        content = r.content
        log.info(f"  Downloaded {len(content):,} bytes from {r.url[-60:]}")
    except Exception as e:
        log.warning(f"  Download failed: {e}")
        return 0

    # Verify it's a real xlsx
    if content[:4] != b"PK\x03\x04":
        log.warning(f"  Not a valid xlsx file (magic: {content[:4].hex()})")
        return 0

    # --- Step 3: parse all sheets --------------------------------------------
    try:
        sheets = pd.read_excel(BytesIO(content), sheet_name=None,
                               header=None, engine="openpyxl")
    except Exception as e:
        log.warning(f"  Could not open workbook: {e}")
        return 0

    log.info(f"  Sheets: {list(sheets.keys())}")
    for sheet_name, df in sheets.items():
        dwelling_type = SHEET_TO_DWELLING.get(sheet_name.lower().strip(), "all")
        rows = _parse_dffh_sheet(df, dwelling_type)
        log.info(f"    Sheet '{sheet_name}' ({dwelling_type}): {len(rows):,} rows")
        all_rows.extend(rows)

    new = upsert("rental_medians", all_rows, ["period", "lga", "suburb", "dwelling_type"])
    log.info(f"Rental: {len(all_rows):,} rows -> {new:,} new inserted")
    return new
