"""
vgv.py -- Valuer-General Victoria connector.

Source: data.vic.gov.au CKAN API  (package_search -- resilient to renames)

VGV XLSX/XLS layout (confirmed from files):
  Row 0:  Title  e.g. "Median House Prices 4th Quarter 2023"
  Row 1:  Headers -- "SUBURB", "Oct - Dec 2022", "Jan - Mar 2023", ..., "No of sales", ...
  Rows 2-6: Blank / continuation rows
  Row 7+:  Data -- suburb name in col 0, quarterly prices in cols 1-N

land.vic.gov.au blocks all bots (403 regardless of User-Agent).
Workaround: Wayback Machine
  - Wayback wrapper URLs in CKAN -> convert to if_ (raw content) format
  - land.vic.gov.au URLs -> query CDX API for archived snapshot -> if_ URL
"""

from __future__ import annotations

import re
import time
from io import BytesIO

import pandas as pd

from .core import build_session, get_logger, upsert

log = get_logger("vgv")

CKAN_BASE   = "https://discover.data.vic.gov.au/api/3/action"
CKAN_SEARCH = f"{CKAN_BASE}/package_search"

VGV_SEARCH_TERMS = [
    "Victorian Property Sales Report median house suburb quarterly",
    "Victorian Property Sales Report median unit suburb quarterly",
    "Victorian Property Sales Report median house suburb time series",
    "Victorian Property Sales Report median unit suburb time series",
    "Victorian Property Sales Report median vacant land suburb time series",
]

DWELLING_KEYWORDS = {
    "house": "house", "houses": "house",
    "unit": "unit",   "units": "unit", "apartment": "unit",
    "land": "land",   "vacant": "land",
}

MONTH_TO_Q = {
    "jan": "Q1", "feb": "Q1", "mar": "Q1",
    "apr": "Q2", "may": "Q2", "jun": "Q2",
    "jul": "Q3", "aug": "Q3", "sep": "Q3",
    "oct": "Q4", "nov": "Q4", "dec": "Q4",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _detect_dwelling_type(text: str) -> str:
    t = text.lower()
    for kw, dtype in DWELLING_KEYWORDS.items():
        if kw in t:
            return dtype
    return "house"


def _detect_period(text: str) -> str | None:
    m = re.search(r"(\d{4})[-_ ]?q(\d)", text, re.I)
    if m:
        return f"{m.group(1)}-Q{m.group(2)}"
    m = re.search(r"(\d{4})", text)
    return m.group(1) if m else None


def _header_to_period(header: str) -> str | None:
    """
    Convert various date range formats to YYYY-QN:
      'Oct - Dec 2023'    -> '2023-Q4'   (older files: spaces around dash)
      'Jan-Mar\n2024'     -> '2024-Q1'   (newer files: newline before year)
      'Jan-Mar 2024'      -> '2024-Q1'
    """
    h = str(header).replace("\n", " ").lower().strip()
    m = re.search(r"([a-z]{3})\s*[-–]\s*[a-z]{3}[\s,]+(\d{4})", h)
    if m:
        quarter = MONTH_TO_Q.get(m.group(1), "")
        if quarter:
            return f"{m.group(2)}-{quarter}"
    return None


def _ckan_search(session, q: str) -> list[dict]:
    try:
        r = session.get(CKAN_SEARCH, params={"q": q, "rows": 10}, timeout=15)
        r.raise_for_status()
        return r.json()["result"]["results"]
    except Exception as e:
        log.warning(f"  CKAN search failed for '{q}': {e}")
        return []


# ---------------------------------------------------------------------------
# Wayback Machine helpers
# ---------------------------------------------------------------------------

def _to_wayback_raw(url: str) -> str | None:
    """Convert Wayback wrapper URL to if_ (raw content) variant."""
    m = re.match(r"(https://web\.archive\.org/web/\d+)/(https?://.*)", url)
    if m:
        return f"{m.group(1)}if_/{m.group(2)}"
    return None


def _wayback_cdx(session, original_url: str) -> str | None:
    """Look up latest successful Wayback snapshot; return if_ URL or None."""
    try:
        r = session.get(
            "https://web.archive.org/cdx/search/cdx",
            params={"url": original_url, "output": "json", "limit": 1,
                    "fl": "timestamp", "filter": "statuscode:200"},
            timeout=15,
        )
        data = r.json()
        if len(data) >= 2:
            ts = data[1][0]
            return f"https://web.archive.org/web/{ts}if_/{original_url}"
    except Exception as e:
        log.debug(f"  CDX lookup failed for {original_url}: {e}")
    return None


def _fetch_file(session, url: str) -> tuple[bytes | None, str]:
    """
    Fetch a spreadsheet file with Wayback Machine fallback.
    Returns (content_bytes, extension) or (None, '').
    """
    ext = url.split("?")[0].rsplit(".", 1)[-1].lower() if "." in url else ""

    # Wayback wrapper URL -> convert to if_
    raw_url = _to_wayback_raw(url)
    if raw_url:
        try:
            r = session.get(raw_url, timeout=60)
            if r.status_code == 200 and len(r.content) > 5000:
                magic = r.content[:4]
                if magic in (b"PK\x03\x04", b"\xd0\xcf\x11\xe0"):
                    return r.content, ext
        except Exception as e:
            log.debug(f"  Wayback if_ error: {e}")
        return None, ext

    # Direct download first
    try:
        r = session.get(url, timeout=60)
        if r.status_code == 200 and len(r.content) > 5000:
            magic = r.content[:4]
            if magic in (b"PK\x03\x04", b"\xd0\xcf\x11\xe0"):
                return r.content, ext
    except Exception:
        pass

    # Wayback CDX fallback
    log.debug(f"  Direct failed, trying Wayback CDX...")
    wb_url = _wayback_cdx(session, url)
    if wb_url:
        try:
            r = session.get(wb_url, timeout=60)
            if r.status_code == 200 and len(r.content) > 5000:
                magic = r.content[:4]
                if magic in (b"PK\x03\x04", b"\xd0\xcf\x11\xe0"):
                    return r.content, ext
        except Exception as e:
            log.debug(f"  Wayback CDX fetch error: {e}")

    return None, ext


# ---------------------------------------------------------------------------
# Sheet parser — VGV-specific wide format
# ---------------------------------------------------------------------------

def _parse_vgv_sheet(df: pd.DataFrame, file_period: str, dwelling_type: str) -> list[dict]:
    """
    Parse a VGV wide-format sheet:
      Col 0 = suburb name
      Cols 1..N = quarterly median prices headed 'Oct - Dec 2023' etc.
      One optional 'No of sales' column for the latest quarter.
    """
    # Find header row: first row where col 0 is 'SUBURB' or 'LOCALITY'
    header_row = None
    for i in range(min(20, len(df))):
        val = str(df.iloc[i, 0]).strip().upper().replace("\n", " ")
        if val in ("SUBURB", "LOCALITY"):
            header_row = i
            break

    if header_row is None:
        # Fallback: look for any row with these keywords anywhere
        for i in range(min(20, len(df))):
            row_str = " ".join(str(v).upper() for v in df.iloc[i] if pd.notna(v))
            if "SUBURB" in row_str or "LOCALITY" in row_str:
                header_row = i
                break

    if header_row is None:
        return []

    headers = [str(df.iloc[header_row, c]).strip() for c in range(df.shape[1])]

    # Map column index -> period for price columns
    price_cols: dict[int, str] = {}
    sales_col: int | None = None

    for i, h in enumerate(headers):
        if i == 0:
            continue  # suburb col
        period = _header_to_period(h)
        if period:
            price_cols[i] = period
        elif "no of sale" in h.lower() or (h.lower().startswith("no") and "sale" in h.lower()):
            if sales_col is None:
                sales_col = i

    if not price_cols:
        # No date-range headers found — sheet is a summary or different format, skip
        return []

    # Find first data row: first row after header where col 0 is non-empty text
    data_start = header_row + 1
    for i in range(header_row + 1, min(header_row + 15, len(df))):
        val = df.iloc[i, 0]
        if pd.notna(val) and str(val).strip() not in ("", "nan"):
            data_start = i
            break

    rows = []
    # Determine the latest period column for attaching num_sales
    latest_price_col = max(price_cols.keys()) if price_cols else None

    for i in range(data_start, len(df)):
        suburb_val = df.iloc[i, 0]
        if pd.isna(suburb_val):
            continue
        suburb_s = str(suburb_val).strip()
        if not suburb_s or suburb_s.lower() in ("nan", "none", "suburb"):
            continue
        # Skip header-area artefacts (month abbreviations like "Oct-Dec")
        if re.match(r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", suburb_s, re.I):
            continue

        for col_idx, period in price_cols.items():
            price_val = df.iloc[i, col_idx]
            if pd.isna(price_val):
                continue
            # '^' means suppressed (< 5 sales) — skip
            if str(price_val).strip() == "^":
                continue
            try:
                price_f = float(str(price_val).replace(",", "").replace("$", "").strip())
            except (ValueError, TypeError):
                continue
            # Skip artefacts: year values (2023, 2024) or impossibly small prices
            if price_f < 50_000:
                continue

            # Attach sales count only for the latest quarter column
            num_sales = None
            if col_idx == latest_price_col and sales_col is not None:
                sales_val = df.iloc[i, sales_col]
                if pd.notna(sales_val):
                    try:
                        num_sales = int(float(str(sales_val).replace(",", "")))
                    except (ValueError, TypeError):
                        pass

            rows.append({
                "period":        period,
                "lga":           "",       # VGV files have no LGA — use "" not None so UNIQUE works
                "suburb":        suburb_s.upper(),
                "dwelling_type": dwelling_type,
                "median_price":  price_f,
                "num_sales":     num_sales,
            })

    return rows


def _parse_vgv_split_header(df: pd.DataFrame, dwelling_type: str) -> list[dict]:
    """
    Parse VGV quarterly files where the date header is split across TWO rows
    (2025+ format):
      Row 0: 'Locality' | NaN | NaN | ...
      Row 1: NaN | 'Jan-Mar' | NaN | 'Apr-Jun' | NaN | ...   (month range)
      Row 2: NaN | '2024'    | NaN | '2024'    | NaN | ...   (year)
      Row 3+: SUBURB | price | count_or_sup | price | ...

    Price columns are at odd indices (1, 3, 5 ...);
    suppressor columns are at even indices (2, 4, 6 ...).
    """
    if len(df) < 4 or df.shape[1] < 3:
        return []

    # Detect: row 0 col 0 = "Locality"
    if str(df.iloc[0, 0]).strip().upper() != "LOCALITY":
        return []

    # Two sub-formats:
    #   A (Mar 2025): row 0 = Locality+NaN, row 1 = months, row 2 = years, data at row 3
    #   B (Jun 2025): row 0 = Locality+months, row 1 = years, data at row 4 (extra blanks)
    month_row, year_row, data_start = None, None, 3
    for candidate_month, candidate_year in [(1, 2), (0, 1)]:
        if df.shape[0] <= candidate_year:
            continue
        cell_m = str(df.iloc[candidate_month, 1]).strip() if pd.notna(df.iloc[candidate_month, 1]) else ""
        cell_y = str(df.iloc[candidate_year,  1]).strip() if pd.notna(df.iloc[candidate_year,  1])  else ""
        if re.match(r"[A-Za-z]{3}[-–]", cell_m):
            try:
                int(float(cell_y))
                month_row, year_row = candidate_month, candidate_year
                data_start = candidate_year + 1
                break
            except (ValueError, TypeError):
                pass

    if month_row is None:
        return []

    # Build column -> period mapping (price cols at 1, 3, 5, ...)
    price_cols: dict[int, str] = {}
    for c in range(1, df.shape[1], 2):
        month_r = str(df.iloc[month_row, c]).strip() if pd.notna(df.iloc[month_row, c]) else ""
        year_r  = str(df.iloc[year_row,  c]).strip() if pd.notna(df.iloc[year_row,  c]) else ""
        if not month_r or not year_r:
            continue
        period = _header_to_period(f"{month_r} {year_r}")
        if period:
            price_cols[c] = period

    if not price_cols:
        return []

    rows = []
    for i in range(data_start, len(df)):
        suburb_val = df.iloc[i, 0]
        if pd.isna(suburb_val):
            continue
        suburb_s = str(suburb_val).strip()
        if not suburb_s or suburb_s.lower() in ("nan", "none", "locality", "suburb"):
            continue
        if re.match(r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", suburb_s, re.I):
            continue

        for col_idx, period in price_cols.items():
            price_val = df.iloc[i, col_idx]
            if pd.isna(price_val) or str(price_val).strip() in ("^", "", "nan"):
                continue
            try:
                price_f = float(str(price_val).replace(",", "").replace("$", "").strip())
            except (ValueError, TypeError):
                continue
            if price_f < 50_000:
                continue

            rows.append({
                "period":        period,
                "lga":           "",
                "suburb":        suburb_s.upper(),
                "dwelling_type": dwelling_type,
                "median_price":  price_f,
                "num_sales":     None,
            })

    return rows


def _parse_vgv_time_series(df: pd.DataFrame, dwelling_type: str) -> list[dict]:
    """
    Parse a VGV time-series sheet (yearly format):
      Row 1:  'Locality' | 2013 | 2014 | ... | 2023  (year integers as headers)
      Row 4+: SUBURB     | price| price| ... | price

    Returns one row per suburb-year with period="YYYY".
    """
    # Find header row with 'Locality'/'Suburb' and integer year columns
    header_row = None
    for i in range(min(10, len(df))):
        val = str(df.iloc[i, 0]).strip().upper()
        if val in ("LOCALITY", "SUBURB"):
            header_row = i
            break

    if header_row is None:
        return []

    headers = [str(df.iloc[header_row, c]).strip() for c in range(df.shape[1])]

    # Map column index -> year string (only valid 4-digit years)
    year_cols: dict[int, str] = {}
    for i, h in enumerate(headers[1:], 1):
        try:
            yr = int(float(h))
            if 2000 <= yr <= 2030:
                year_cols[i] = str(yr)
        except (ValueError, TypeError):
            pass

    if not year_cols:
        return []

    # Find first data row
    data_start = header_row + 1
    for i in range(header_row + 1, min(header_row + 8, len(df))):
        val = df.iloc[i, 0]
        if pd.notna(val) and str(val).strip() not in ("", "nan"):
            data_start = i
            break

    rows = []
    for i in range(data_start, len(df)):
        suburb_val = df.iloc[i, 0]
        if pd.isna(suburb_val):
            continue
        suburb_s = str(suburb_val).strip()
        if not suburb_s or suburb_s.lower() in ("nan", "none", "suburb", "locality"):
            continue
        if re.match(r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", suburb_s, re.I):
            continue

        for col_idx, year_str in year_cols.items():
            price_val = df.iloc[i, col_idx]
            if pd.isna(price_val) or str(price_val).strip() in ("^", "n/a", "na", ""):
                continue
            try:
                price_f = float(str(price_val).replace(",", "").replace("$", "").strip())
            except (ValueError, TypeError):
                continue
            if price_f < 50_000:
                continue

            rows.append({
                "period":        year_str,          # "2013", "2014", ..., "2023"
                "lga":           "",
                "suburb":        suburb_s.upper(),
                "dwelling_type": dwelling_type,
                "median_price":  price_f,
                "num_sales":     None,
            })

    return rows


def _parse_workbook(content: bytes, ext: str, file_period: str, dwelling_type: str) -> list[dict]:
    """Parse a VGV workbook (.xls or .xlsx).
    Tries quarterly wide-format first; falls back to yearly time-series format."""
    try:
        engine = "xlrd" if ext == "xls" else "openpyxl"
        sheets = pd.read_excel(BytesIO(content), sheet_name=None, header=None, engine=engine)
    except Exception as e:
        log.warning(f"  Workbook parse error ({ext}): {e}")
        return []

    all_rows = []
    for sheet_name, df in sheets.items():
        s_period = _detect_period(str(sheet_name)) or file_period
        s_type   = _detect_dwelling_type(str(sheet_name)) if sheet_name else dwelling_type

        # 1. Quarterly wide format  (e.g. "Oct - Dec 2023" headers, 2023-2024 files)
        rows = _parse_vgv_sheet(df, s_period, s_type)

        # 2. Split-header quarterly format (2025+ files: month on row 1, year on row 2)
        if not rows:
            rows = _parse_vgv_split_header(df, s_type)

        # 3. Yearly time-series format  (integer year headers, 2013-2023 files)
        if not rows:
            rows = _parse_vgv_time_series(df, s_type)

        all_rows.extend(rows)
        if rows:
            log.debug(f"    Sheet '{sheet_name}': {len(rows)} rows")

    return all_rows


# ---------------------------------------------------------------------------
# Resource processing
# ---------------------------------------------------------------------------

def _process_resource(session, resource: dict) -> list[dict]:
    url  = resource.get("url", "")
    name = resource.get("name", "") or resource.get("description", "")

    if not url:
        return []

    url_lower = url.lower().split("?")[0]
    is_spreadsheet = any(url_lower.endswith(ext) for ext in (".xlsx", ".xls"))
    is_wayback     = "web.archive.org" in url

    if not is_spreadsheet and not is_wayback:
        return []

    file_period   = _detect_period(url) or _detect_period(name) or "unknown"
    dwelling_type = _detect_dwelling_type(url + " " + name)

    log.info(f"  Fetching: {name or url[-70:]}")
    content, ext = _fetch_file(session, url)

    if content is None:
        log.warning(f"  Could not retrieve: {url[-70:]}")
        return []

    # Infer ext from magic bytes if missing
    if not ext or ext not in ("xls", "xlsx"):
        ext = "xls" if content[:4] == b"\xd0\xcf\x11\xe0" else "xlsx"

    rows = _parse_workbook(content, ext, file_period, dwelling_type)
    log.info(f"    -> {len(rows)} rows")
    return rows


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run() -> int:
    session  = build_session()
    all_rows = []
    seen_pkg = set()

    for search_term in VGV_SEARCH_TERMS:
        log.info(f"CKAN search: '{search_term}'")
        packages = _ckan_search(session, search_term)

        if not packages:
            log.warning(f"  No packages found")
            continue

        for pkg in packages:
            pkg_id = pkg.get("id") or pkg.get("name")
            if pkg_id in seen_pkg:
                continue
            seen_pkg.add(pkg_id)

            resources = pkg.get("resources", [])
            log.info(f"  Package: {pkg.get('title', pkg_id)} -- {len(resources)} resources")

            for resource in resources:
                rows = _process_resource(session, resource)
                all_rows.extend(rows)
                time.sleep(0.5)

    new = upsert("sales_medians", all_rows, ["period", "lga", "suburb", "dwelling_type"])
    log.info(f"VGV: {len(all_rows)} rows -> {new} new inserted")
    return new
