"""
drivers.py -- state demand-side drivers connector (ABS ERP_COMP_Q).

Source: ABS Data API, dataflow ERP_COMP_Q (Population and components of change,
        national/state/territory, quarterly). Free.

Housing prices are supply + demand. The pipeline already captures supply
(building approvals); this adds the DEMAND drivers for the state:
  - net interstate migration   (the #1 cross-state divergence factor)
  - net overseas migration
  - natural increase (births - deaths)
  - total quarterly population change

(A cross-state panel test found these explain the COMMON cycle weakly and
cross-state DIVERGENCE barely at all -- migration is partly endogenous to price
-- but they remain essential context for any single-state demand analysis.)
"""
from __future__ import annotations

import json

from .core import build_session, get_logger, upsert

log = get_logger("drivers")

ERP_URL = "https://data.api.abs.gov.au/rest/data/ABS,ERP_COMP_Q,1.0.0/all"
SDMX_JSON = "application/vnd.sdmx.data+json;version=1.0"
STATE = "Victoria"          # demand drivers for this state
START = "2011-Q1"

MEASURE_MAP = {
    "net internal migration": "net_interstate_migration",
    "net overseas migration": "net_overseas_migration",
    "natural increase":       "natural_increase",
    "change over previous quarter": "population_change",
}


def run() -> int:
    session = build_session()
    log.info(f"Fetching ABS ERP_COMP_Q demand drivers for {STATE}")
    try:
        r = session.get(ERP_URL, headers={"Accept": SDMX_JSON},
                        params={"startPeriod": START}, timeout=120, stream=True)
        r.raise_for_status()
        data = json.loads(b"".join(r.iter_content(65536)))
    except Exception as e:
        log.warning(f"  ERP_COMP_Q fetch error: {e}")
        return 0

    sd = data["data"]["structure"]["dimensions"]["series"]
    ids = [d["id"] for d in sd]
    meas_i, reg_i = ids.index("MEASURE"), ids.index("REGION")
    meas, region = sd[meas_i], sd[reg_i]
    times = data["data"]["structure"]["dimensions"]["observation"][0]["values"]

    rows = []
    for skey, sdata in data["data"]["dataSets"][0]["series"].items():
        keys = skey.split(":")
        if region["values"][int(keys[reg_i])]["name"] != STATE:
            continue
        code = MEASURE_MAP.get(meas["values"][int(keys[meas_i])]["name"].lower())
        if code is None:
            continue
        for tk, ov in sdata.get("observations", {}).items():
            if ov and ov[0] is not None:
                rows.append({"period": times[int(tk)]["id"], "measure": code,
                             "value": float(ov[0])})

    new = upsert("state_drivers", rows, ["period", "measure"])
    log.info(f"Drivers ({STATE}): {len(rows)} rows -> {new} new inserted")
    return new

