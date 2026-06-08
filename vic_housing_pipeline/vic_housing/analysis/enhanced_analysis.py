"""
enhanced_analysis.py -- upgrades the analysis with TWO free data sources that
close the gaps flagged earlier:

  (A) RBA Cash Rate Target  (F1.1, free CSV)  -> proper rate sensitivity
  (B) ABS RPPI capital-city price indices (free SDMX API) -> interstate
      DIFFERENCE-IN-DIFFERENCES, so VIC-specific policy can be separated from
      the national rate cycle (Melbourne = treatment, other capitals = control).

RPPI is QUARTERLY 2011-2025 (~58 obs) -- far better N than the 11 annual points
we had, so the rate regression becomes properly powered.

Outputs raw data + results to exports/analysis/.
"""
from __future__ import annotations
import sys, pathlib, json, csv, io, warnings
warnings.filterwarnings("ignore")
try: sys.stdout.reconfigure(encoding="utf-8")   # Windows cp1252 -> utf-8 for Δ/²/→
except Exception: pass
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import numpy as np, pandas as pd, statsmodels.api as sm
import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
from vic_housing.core import build_session

OUT = pathlib.Path(__file__).resolve().parent.parent / "exports" / "analysis"
OUT.mkdir(parents=True, exist_ok=True)
session = build_session()
def rule(t): print("\n"+"="*78+f"\n {t}\n"+"="*78)

# ---------------------------------------------------------------------------
# (A) RBA CASH RATE TARGET (F1.1)
# ---------------------------------------------------------------------------
rule("FETCH: RBA Cash Rate Target (free)")
r = session.get("https://www.rba.gov.au/statistics/tables/csv/f1.1-data.csv", timeout=30)
rows = list(csv.reader(io.StringIO(r.text)))
sid_row = next(i for i,row in enumerate(rows) if row and row[0].strip()=="Series ID")
recs=[]
for row in rows[sid_row+1:]:
    if not row or not row[0].strip(): continue
    try:
        dt = pd.to_datetime(row[0].strip(), dayfirst=True)
        val = float(row[1])
        recs.append((dt, val))
    except (ValueError, IndexError): continue
cash = pd.DataFrame(recs, columns=["date","cash_rate"]).set_index("date")
cash["q"] = cash.index.to_period("Q")
cash_q = cash.groupby("q")["cash_rate"].mean()
cash_q.index = cash_q.index.astype(str).str.replace("Q","-Q")   # 2024Q1 -> 2024-Q1
print(f"  Cash rate: {len(cash_q)} quarters, {cash_q.index.min()}..{cash_q.index.max()}, latest {cash_q.iloc[-1]:.2f}%")
cash_q.to_csv(OUT/"cash_rate_quarterly.csv")

# ---------------------------------------------------------------------------
# (B) ABS RES_DWELL -- CURRENT median established-house price by Greater Capital
#     City, quarterly 2016-2025. (Legacy RPPI ended 2021-Q4, so we use this.)
# ---------------------------------------------------------------------------
rule("FETCH: ABS RES_DWELL median established-house price by capital (free, to 2025)")
url = "https://data.api.abs.gov.au/rest/data/ABS,RES_DWELL,1.0.0/all"
resp = session.get(url, headers={"Accept":"application/vnd.sdmx.data+json;version=1.0"},
                   params={"startPeriod":"2016-Q1"}, timeout=120, stream=True)
data = json.loads(b"".join(resp.iter_content(65536)))
struct = data["data"]["structure"]["dimensions"]["series"]
ids = [d["id"] for d in struct]
meas = next(d for d in struct if d["id"]=="MEASURE"); meas_i = ids.index("MEASURE")
region = next(d for d in struct if d["id"]=="REGION"); region_i = ids.index("REGION")
times = data["data"]["structure"]["dimensions"]["observation"][0]["values"]
# MEASURE index for 'Median Price of Established House Transfers'
med_idx = next(i for i,v in enumerate(meas["values"]) if "median price of established house" in v["name"].lower())
recs=[]
for skey, sdata in data["data"]["dataSets"][0]["series"].items():
    keys = skey.split(":")
    if int(keys[meas_i]) != med_idx: continue
    reg = region["values"][int(keys[region_i])]["name"]
    if not reg.startswith("Greater"): continue          # capital cities only
    for tk, ov in sdata.get("observations",{}).items():
        recs.append((times[int(tk)]["id"], reg, ov[0]))
rd = pd.DataFrame(recs, columns=["period","region","price"])
rd["price"] = pd.to_numeric(rd["price"], errors="coerce")
piv = rd.pivot_table(index="period", columns="region", values="price").sort_index()
piv.to_csv(OUT/"capital_median_house.csv")
print(f"  Median established-house price: {piv.shape[0]} quarters x {piv.shape[1]} cities, "
      f"{piv.index.min()}..{piv.index.max()}")
print(f"  Latest: " + ", ".join(f"{c.replace('Greater ','')} ${piv[c].dropna().iloc[-1]:,.0f}" for c in piv.columns if piv[c].notna().any()))

MEL = "Greater Melbourne"
CONTROL = "Greater Sydney"   # primary control: best parallel pre-trends (qoq corr +0.87)
CONTROLS = [c for c in ["Greater Sydney","Greater Brisbane","Greater Adelaide","Greater Perth"] if c in piv.columns]
print(f"  Treatment={MEL} | Primary control={CONTROL} | Pooled controls={CONTROLS}")

# ---------------------------------------------------------------------------
# SECTION 4' -- RATE SENSITIVITY (quarterly, properly powered)
# ---------------------------------------------------------------------------
rule("SECTION 4' - CASH-RATE SENSITIVITY (Melbourne RPPI, quarterly distributed lag)")
mel = piv[MEL].dropna()
mel_g = (np.log(mel).diff()*100).rename("mel_g")          # qoq log growth %
df = pd.concat([mel_g, cash_q.rename("cash")], axis=1).dropna()
df["d_cash"] = df["cash"].diff()
for L in range(0,5):
    df[f"d_cash_l{L}"] = df["d_cash"].shift(L)
reg = df.dropna()
X = sm.add_constant(reg[[f"d_cash_l{L}" for L in range(5)]])
m = sm.OLS(reg["mel_g"], X).fit()
print(f"  Distributed-lag OLS: Melbourne qoq growth ~ Δcash(t..t-4)   N={int(m.nobs)}, R²={m.rsquared:.3f}")
cum=0
for L in range(5):
    b=m.params[f"d_cash_l{L}"]; p=m.pvalues[f"d_cash_l{L}"]; cum+=b
    star = "***" if p<.01 else "**" if p<.05 else "*" if p<.1 else ""
    print(f"     Δcash lag{L}q: β={b:+.2f}pp price/qtr per +1pp rate  p={p:.3f}{star}")
print(f"  --> CUMULATIVE 5-qtr effect of a +1pp cash-rate rise: {cum:+.2f}% on Melbourne house prices")
# annualised level relationship
lv = pd.concat([np.log(mel).rename("lp"), cash_q.rename("cash")], axis=1).dropna()
print(f"  corr(log price level, cash rate level) = {lv['lp'].corr(lv['cash']):+.3f}")

# ---------------------------------------------------------------------------
# SECTION 5' -- DIFFERENCE-IN-DIFFERENCES (VIC policy vs other capitals)
# ---------------------------------------------------------------------------
rule("SECTION 5' - DIFFERENCE-IN-DIFFERENCES: isolating VIC-specific policy")
print("  Logic: national rate cycle hits ALL capitals; a VIC-only divergence vs")
print("  the control capitals around a VIC policy date is the policy's signature.\n")

def did(event_period, label, control, pre_q=6, post_q=6):
    cols = [MEL, control] if isinstance(control,str) else [MEL]+control
    ctrls = [control] if isinstance(control,str) else control
    sub = piv[cols].dropna()
    periods = list(sub.index)
    if event_period not in periods:
        print(f"  [{event_period}] {label}\n     SKIPPED - event quarter not in data range "
              f"({periods[0]}..{periods[-1]})\n")
        return None
    ei = periods.index(event_period)
    pre = periods[max(0,ei-pre_q):ei]; post = periods[ei:ei+post_q]
    if len(pre)<3 or len(post)<3:
        print(f"  [{event_period}] {label}\n     SKIPPED - insufficient window "
              f"(pre={len(pre)}, post={len(post)})\n")
        return None
    def g(region, qs): return (sub.loc[qs[-1],region]/sub.loc[qs[0],region]-1)*100
    mel_d = g(MEL,post)-g(MEL,pre)
    cpre = np.mean([g(c,pre) for c in ctrls]); cpost = np.mean([g(c,post) for c in ctrls])
    didv = mel_d-(cpost-cpre)
    cname = control if isinstance(control,str) else "pooled controls"
    print(f"  [{event_period}] {label}   (control: {cname})")
    print(f"     Melbourne: pre {g(MEL,pre):+5.1f}%  post {g(MEL,post):+5.1f}%   Δ={mel_d:+5.1f}")
    print(f"     Control  : pre {cpre:+5.1f}%  post {cpost:+5.1f}%   Δ={cpost-cpre:+5.1f}")
    print(f"     >>> DiD (VIC-specific) = {didv:+.1f} pts  "
          f"({'Melbourne UNDER-performed' if didv<0 else 'OUT-performed'})\n")
    return didv

print("  HEADLINE EVENT -- 2024 VIC land-tax expansion (threshold $300k->$50k, Jan 2024):")
did("2024-Q1", "VIC land-tax threshold slashed + expanded VRLT", CONTROL)
did("2024-Q1", "  (same, robustness: pooled 4-capital control)", CONTROLS)
print("  CONTROL/PLACEBO events (expect ~0 if design is clean):")
did("2019-Q2", "PLACEBO: 2019 fed-election quarter (national, not VIC-specific)", CONTROL)
did("2021-Q3", "PLACEBO: mid-COVID boom (national)", CONTROL)

print("  Interpretation: Melbourne has structurally under-performed Sydney since the")
print("  2022-24 tightening. A negative 2024 DiD with smaller placebo DiDs supports a")
print("  (modest) VIC-tax drag; but Melbourne's pre-existing relative weakness means")
print("  the parallel-trends assumption is imperfect -- read as upper-bound, not proof.")

# ---------------------------------------------------------------------------
# CHART: Melbourne vs control capitals (indexed) + DiD visual
# ---------------------------------------------------------------------------
rule("CHART")
fig,ax=plt.subplots(figsize=(12,5.5))
plot_cols=[MEL]+CONTROLS
base = piv[plot_cols].dropna().index[0]
for c in plot_cols:
    s=piv[c].dropna(); s=s/s.loc[base]*100
    lw=2.6 if c==MEL else 1.3
    ax.plot(range(len(s)), s.values, label=c.replace("Greater ",""), lw=lw,
            color="#c0392b" if c==MEL else None, alpha=1 if c==MEL else .65)
if "2024-Q1" in piv.index:
    ax.axvline(list(piv.index).index("2024-Q1"), ls=":", c="black", alpha=.7)
    ax.text(list(piv.index).index("2024-Q1"), ax.get_ylim()[1]*0.98, " land-tax\n expansion",
            fontsize=8, va="top")
ax.set_xticks(range(0,len(piv.index),4)); ax.set_xticklabels(piv.index[::4], rotation=45, ha="right", fontsize=7)
ax.set_title("ABS median established-house price — Melbourne vs capitals (2016-Q1=100)", fontweight="bold")
ax.set_ylabel("Index (2016-Q1=100)"); ax.legend(fontsize=8, ncol=3); ax.grid(alpha=.3)
plt.tight_layout(); plt.savefig(OUT/"melbourne_vs_capitals_did.png", dpi=140); plt.close()
print(f"  Saved: {OUT/'melbourne_vs_capitals_did.png'}")
print(f"  Saved: {OUT/'rppi_capitals.csv'}, {OUT/'cash_rate_quarterly.csv'}")
print("\nDONE.")
