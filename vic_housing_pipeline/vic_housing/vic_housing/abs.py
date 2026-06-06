"""
abs.py — ABS Building Approvals connector (8731.0).

Source: ABS Data API (SDMX-JSON) + XLSX fallback
Cadence: Monthly

SDMX endpoint:
  https://data.api.abs.gov.au/rest/data/ABS,BA_SA3,1.0.0/all
  ?startPeriod=2020-01&detail=Full&dimensionAtObservation=TIME_PERIOD

Falls back to downloading the main XLSX from the ABS website if the API
returns an unexpected response.
"""

from __future__ import annotations

import re
from io import BytesIO

from .core import build_session, cached_get, get_logger, upsert

log = get_logger("abs")

ABS_API_BASE = "https://data.api.abs.gov.au/rest/data"
# Building Approvals by SA3 (most granular publicly available)
DATAFLOW_ID  = "ABS,BA_SA3,1.0.0"
START_PERIOD = "2015-01"

# XLSX fallback — table 8 (approvals by state, monthly, original)
ABS_XLSX_URL = (
    "https://www.abs.gov.au/statistics/industry/building-and-construction/"
    "building-approvals-australia/latest-release/87310do001_202401.xlsx"
)

REGION_LABEL = "Victoria"


def _parse_sdmx(data: dict) -> list[dict]:
    """Parse SDMX-JSON response into flat rows."""
    rows = []
    try:
        structure   = data["data"]["structure"]
        dimensions  = structure["dimensions"]["observation"]
        # Find time, region, type dimension indices
        dim_map = {d["id"]: i for i, d in enumerate(dimensions)}
        time_idx   = dim_map.get("TIME_PERIOD", 0)
        region_idx = dim_map.get("SA3", None)
        type_idx   = dim_map.get("DWELLING_TYPE", None)

        observations = data["data"]["dataSets"][0]["observations"]
        for key_str, vals in observations.items():
            keys   = key_str.split(":")
            period = keys[time_idx] if time_idx < len(keys) else "unknown"
            region = (
                dimensions[region_idx]["values"][int(keys[region_idx])]["name"]
                if region_idx is not None and int(keys[region_idx]) < len(dimensions[region_idx]["values"])
                else "Victoria"
            )
            dtype = (
                dimensions[type_idx]["values"][int(keys[type_idx])]["name"].lower()
                if type_idx is not None
                else "total"
            )

            value = vals[0] if vals else None
            if value is None:
                continue

            rows.append({
                "period":        period,
                "region":        region,
                "dwelling_type": dtype,
                "seasonality":   "original",
                "num_approvals": int(value),
                "value_000":     None,
            })
    except Exception as e:
        log.warning(f"SDMX parse error: {e}")
    return rows


def _parse_xlsx_fallback(content: bytes) -> list[dict]:
    """Minimal fallback parser for the ABS 8731 XLSX table."""
    import openpyxl
    rows = []
    try:
        wb = openpyxl.load_workbook(BytesIO(content), read_only=True, data_only=True)
        for ws in wb.worksheets:
            if "victoria" not in ws.title.lower() and "table" not in ws.title.lower():
                continue
            # Find header row
            header_row = None
            for r in ws.iter_rows(max_row=30):
                for cell in r:
                    if "month" in str(cell.value or "").lower() or "period" in str(cell.value or "").lower():
                        header_row = cell.row
                        break
                if header_row:
                    break

            if not header_row:
                continue

            for r in range(header_row + 1, ws.max_row + 1):
                period = ws.cell(r, 1).value
                val    = ws.cell(r, 2).value
                if not period or not val:
                    continue
                # Normalise period to YYYY-MM
                period_str = str(period).strip()
                m = re.search(r"(\d{4})[-/](\d{1,2})", period_str)
                if m:
                    period_str = f"{m.group(1)}-{int(m.group(2)):02d}"
                else:
                    continue
                try:
                    rows.append({
                        "period":        period_str,
                        "region":        "Victoria",
                        "dwelling_type": "total",
                        "seasonality":   "original",
                        "num_approvals": int(float(str(val).replace(",", ""))),
                        "value_000":     None,
                    })
                except ValueError:
                    continue
    except Exception as e:
        log.warning(f"XLSX fallback parse error: {e}")
    return rows


def run() -> int:
    session = build_session()
    rows    = []

    # --- Attempt SDMX API ---
    log.info("Fetching ABS building approvals via SDMX API")
    url  = f"{ABS_API_BASE}/{DATAFLOW_ID}/all"
    resp = cached_get(session, url, params={
        "startPeriod":            START_PERIOD,
        "detail":                 "Full",
        "dimensionAtObservation": "TIME_PERIOD",
    })
    if resp is not None:
        try:
            data = resp.json()
            rows = _parse_sdmx(data)
            log.info(f"  SDMX: parsed {len(rows)} observations")
        except Exception as e:
            log.warning(f"  SDMX JSON parse failed: {e}")

    # --- Fallback to XLSX if SDMX returned nothing ---
    if not rows:
        log.info("Falling back to ABS XLSX download")
        resp = cached_get(session, ABS_XLSX_URL)
        if resp is not None:
            rows = _parse_xlsx_fallback(resp.content)
            log.info(f"  XLSX fallback: parsed {len(rows)} rows")

    new = upsert("building_approvals", rows,
                 ["period", "region", "dwelling_type", "seasonality"])
    log.info(f"ABS: {len(rows)} rows parsed → {new} new inserted")
    return new
