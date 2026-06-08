"""
capitals.py -- ABS interstate capital-city dwelling-price connector.

Source: ABS Data API, dataflow RES_DWELL (Residential Dwellings: Unstratified
        Medians and Transfer Counts by Dwelling Type, GCCSA and Rest of State).
        Free SDMX-JSON, quarterly, 2016-present.

Why: gives median established-house & attached-dwelling prices for ALL capital
cities -> the interstate counterfactual needed for difference-in-differences
analysis of Victoria-specific policy (Melbourne = treatment, other capitals =
control). The national rate cycle is common to all capitals and differences out.
"""
from __future__ import annotations

import json

from .core import build_session, get_logger, upsert

log = get_logger("capitals")

RES_DWELL_URL = "https://data.api.abs.gov.au/rest/data/ABS,RES_DWELL,1.0.0/all"
SDMX_JSON = "application/vnd.sdmx.data+json;version=1.0"
START = "2011-Q1"

# Map ABS MEASURE names -> our compact measure codes
MEASURE_MAP = {
    "median price of established house transfers": "median_house",
    "median price of attached dwelling transfers": "median_unit",
    "number of established house transfers":       "count_house",
    "number of attached dwelling transfers":       "count_unit",
}


def _fetch_json(session) -> dict | None:
    try:
        r = session.get(RES_DWELL_URL, headers={"Accept": SDMX_JSON},
                        params={"startPeriod": START}, timeout=120, stream=True)
        r.raise_for_status()
        return json.loads(b"".join(r.iter_content(65536)))
    except Exception as e:
        log.warning(f"  RES_DWELL fetch error: {e}")
        return None


def run() -> int:
    session = build_session()
    log.info("Fetching ABS RES_DWELL (interstate capital-city dwelling prices)")
    data = _fetch_json(session)
    if not data:
        return 0

    try:
        sdims = data["data"]["structure"]["dimensions"]["series"]
        ids = [d["id"] for d in sdims]
        meas = sdims[ids.index("MEASURE")]
        region = sdims[ids.index("REGION")]
        meas_i, region_i = ids.index("MEASURE"), ids.index("REGION")
        times = data["data"]["structure"]["dimensions"]["observation"][0]["values"]
    except (KeyError, ValueError) as e:
        log.warning(f"  RES_DWELL structure parse error: {e}")
        return 0

    rows = []
    for skey, sdata in data["data"]["dataSets"][0]["series"].items():
        keys = skey.split(":")
        meas_name = meas["values"][int(keys[meas_i])]["name"].lower()
        code = MEASURE_MAP.get(meas_name)
        if code is None:
            continue
        reg = region["values"][int(keys[region_i])]["name"]
        for tk, ov in sdata.get("observations", {}).items():
            val = ov[0] if ov else None
            if val is None:
                continue
            rows.append({
                "period": times[int(tk)]["id"],
                "region": reg,
                "measure": code,
                "value": float(val),
            })

    new = upsert("capital_prices", rows, ["period", "region", "measure"])
    log.info(f"Capitals: {len(rows)} rows parsed -> {new} new inserted")
    return new
