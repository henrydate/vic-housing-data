"""
policy_event_study.py -- Interrupted-time-series / event study of Victorian &
federal housing-policy changes against the statewide median house-price series.

CRITICAL HONESTY: the repo contains NO policy database. Policy dates below are
hand-coded from public record (external knowledge), flagged as such. With only
aggregated annual/quarterly median prices and no control group, this is
DESCRIPTIVE association around event windows -- NOT identified causal effect.
Interest rates and COVID confound nearly every window and are noted per event.
"""
from __future__ import annotations
import sys, pathlib, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import numpy as np, pandas as pd
from vic_housing.core import get_conn

conn = get_conn()
sales = pd.read_sql_query("SELECT period, suburb, median_price FROM sales_medians WHERE dwelling_type='house'", conn)
oo    = pd.read_sql_query("SELECT period, rate_pct FROM lending_rates WHERE series_id='F5/FILRHLBVS'", conn)
conn.close()
sales["median_price"] = pd.to_numeric(sales["median_price"], errors="coerce")

# Annual statewide index (median of suburb medians)
sales["year"] = sales["period"].str[:4].astype(int)
ann = sales.groupby("year")["median_price"].median()
ann_g = ann.pct_change()*100
oo["year"] = oo["period"].str[:4].astype(int)
rate_y = oo.groupby("year")["rate_pct"].mean()

def rule(t): print("\n"+"="*78+f"\n {t}\n"+"="*78)

rule("SECTION 5 - POLICY EVENT STUDY (annual statewide house-price growth)")
print("  Policy dates = external public record (NOT in repo data). Confounded by")
print("  rates/COVID. Windows show 2yr-pre vs 2yr-post MEAN annual price growth.\n")

# (year_effective, label, jurisdiction, expected_sign, confounder)
events = [
    (2016, "Foreign purchaser duty 3%->7%",            "VIC", "-", "AU-wide boom; APRA IO curbs"),
    (2017, "FHB stamp-duty exempt <$600k; VRLT announced","VIC","+ (demand) / - (invest)", "2017 peak, then APRA-driven 2018 downturn"),
    (2019, "Fed election: negative-gearing/CGT reform DEFEATED","FED","+", "Rate cuts Jun/Jul/Oct 2019; APRA 7% buffer removed"),
    (2020, "HomeBuilder $25k grant",                    "FED", "+", "COVID shock + emergency 0.10% cash rate"),
    (2023, "Windfall Gains Tax live; COVID-debt land-tax levy","VIC","-", "RBA hiking cycle 2022-23 (biggest confounder)"),
    (2024, "Land-tax threshold $300k->$50k",            "VIC", "-", "Rates near peak; immigration-driven demand"),
]
print(f"  {'Eff.Yr':6} {'Policy':52} {'Pre2y':>7} {'Post2y':>7} {'Delta':>7}  Sign")
print("  "+"-"*94)
rows=[]
for yr,label,juris,sign,conf in events:
    pre  = ann_g.loc[(ann_g.index>=yr-2)&(ann_g.index<=yr-1)].mean()
    post = ann_g.loc[(ann_g.index>=yr)&(ann_g.index<=yr+1)].mean()
    d = post-pre if (pd.notna(pre) and pd.notna(post)) else np.nan
    rows.append((yr,label,juris,pre,post,d,sign,conf))
    ps = f"{pre:6.1f}%" if pd.notna(pre) else "   n/a"
    qs = f"{post:6.1f}%" if pd.notna(post) else "   n/a"
    ds = f"{d:+6.1f}" if pd.notna(d) else "  n/a"
    print(f"  {yr:6} {label[:52]:52} {ps:>7} {qs:>7} {ds:>7}  {sign}")
print("\n  Per-event confounder (why causal attribution is weak):")
for yr,label,juris,pre,post,d,sign,conf in rows:
    print(f"    {yr} [{juris}] {label[:46]:46} -> CONFOUND: {conf}")

# Rate context alongside
rule("RATE CONTEXT around each event (the dominant confounder)")
print(f"  {'Year':6}{'PriceGrowth':>13}{'OO Rate':>10}{'dRate':>8}")
for y in range(2014, 2026):
    pg = ann_g.get(y, np.nan); rt = rate_y.get(y, np.nan)
    dr = rate_y.get(y,np.nan)-rate_y.get(y-1,np.nan)
    pgs=f"{pg:+.1f}%" if pd.notna(pg) else "n/a"
    rts=f"{rt:.2f}%" if pd.notna(rt) else "n/a"
    drs=f"{dr:+.2f}" if pd.notna(dr) else "n/a"
    print(f"  {y:6}{pgs:>13}{rts:>10}{drs:>8}")

print("\n  READ: 2023-24 land-tax tightening coincides with the post-hike REBOUND,")
print("  i.e. prices rose DESPITE investor-negative policy -> demand/rate/immigration")
print("  channels dominated policy. This is exactly why single-series event studies")
print("  cannot isolate policy here; a DiD with interstate controls would be needed")
print("  (interstate price data NOT in this repo).")
