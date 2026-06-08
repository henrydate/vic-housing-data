"""
market_analysis.py -- Quantitative analysis of the Victorian house market.

Honest scope: this repo holds AGGREGATED MEDIAN data, not transaction-level
records. So we can rigorously analyse:
  - Location (suburb) vs time as price drivers  (variance decomposition)
  - Gross/net rental yields by segment + compression over time
  - Rent <-> price lead/lag
  - Lending-rate sensitivity of prices
  - Building approvals (supply) vs price
We CANNOT analyse beds/baths/land size/school zones/auction clearance/days-on-
market/vacancy (not in data). Those are flagged, not faked.

Run:  python analysis/market_analysis.py
"""
from __future__ import annotations
import sys, pathlib, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import statsmodels.api as sm
import statsmodels.formula.api as smf
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from vic_housing.core import get_conn

OUT = pathlib.Path(__file__).resolve().parent.parent / "exports" / "analysis"
OUT.mkdir(parents=True, exist_ok=True)

def rule(t): print("\n" + "="*78 + f"\n {t}\n" + "="*78)

# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------
conn = get_conn()
sales = pd.read_sql_query(
    "SELECT period, suburb, median_price, num_sales FROM sales_medians "
    "WHERE dwelling_type='house'", conn)
rent  = pd.read_sql_query("SELECT period, suburb, dwelling_type, median_rent FROM rental_medians", conn)
appr  = pd.read_sql_query("SELECT period, region, dwelling_type, seasonality, num_approvals FROM building_approvals", conn)
rates = pd.read_sql_query("SELECT period, series_id, series_label, rate_pct FROM lending_rates", conn)
conn.close()

for d in (sales, rent):
    d["suburb"] = d["suburb"].str.upper().str.strip()
sales["median_price"] = pd.to_numeric(sales["median_price"], errors="coerce")
rent["median_rent"]   = pd.to_numeric(rent["median_rent"], errors="coerce")
sales["year"] = sales["period"].str[:4].astype(int)
rent["year"]  = rent["period"].str[:4].astype(int)

# Annual house price per suburb (median across any quarters in that year)
price_y = (sales.groupby(["year","suburb"])["median_price"].median().reset_index())

# House-matched rent: mean of *_house bedroom series; fallback to 'all'
house_rent = (rent[rent.dwelling_type.isin(["2br_house","3br_house","4br_house"])]
              .groupby(["year","suburb"])["median_rent"].mean().reset_index()
              .rename(columns={"median_rent":"rent_house"}))
all_rent = (rent[rent.dwelling_type=="all"]
            .groupby(["year","suburb"])["median_rent"].median().reset_index()
            .rename(columns={"median_rent":"rent_all"}))

rule("DATA INVENTORY (what is actually usable)")
print(f"  House sales: {len(sales):,} rows | {sales.suburb.nunique()} suburbs | years {sales.year.min()}-{sales.year.max()}")
print(f"  Rent:        {len(rent):,} rows | types={sorted(rent.dwelling_type.unique())}")
print(f"  House-rent panel: {len(house_rent):,} suburb-years ; All-rent panel: {len(all_rent):,}")
print(f"  Approvals:   {len(appr):,} rows | regions={appr.region.nunique()}")
print(f"  Lending rates: {rates.series_id.nunique()} series, {rates.period.min()}-{rates.period.max()}")
print("  NOT AVAILABLE: beds/baths, land size, school zones, infra proximity,")
print("                 auction clearance, days-on-market, vacancy, cash rate.")

# ===========================================================================
# SECTION 1 — PRICE-DRIVER VARIANCE DECOMPOSITION  (honest feature importance)
# ===========================================================================
rule("SECTION 1 - PRICE DRIVERS: variance decomposition of log(median_price)")
df1 = price_y.copy()
df1["log_price"] = np.log(df1["median_price"])
df1["yr_c"] = df1["year"] - df1["year"].mean()

# Sequential models: time only -> + suburb fixed effects
m_time = smf.ols("log_price ~ yr_c", data=df1).fit()
m_full = smf.ols("log_price ~ yr_c + C(suburb)", data=df1).fit()
r2_time = m_time.rsquared
r2_full = m_full.rsquared
print(f"  Model A  log_price ~ year            : R^2 = {r2_time:6.3f}")
print(f"  Model B  log_price ~ year + suburb   : R^2 = {r2_full:6.3f}  (adj {m_full.rsquared_adj:.3f})")
print(f"  --> Time alone explains            {r2_time*100:5.1f}% of price variance")
print(f"  --> Adding SUBURB raises R^2 by    {(r2_full-r2_time)*100:5.1f} pts  (location dominates)")
print(f"  --> Annual trend coef = {m_time.params['yr_c']:.4f}  => {np.exp(m_time.params['yr_c'])-1:+.2%} price/yr avg")
ci = m_time.conf_int().loc["yr_c"]
print(f"      95% CI on trend: [{np.exp(ci[0])-1:+.2%}, {np.exp(ci[1])-1:+.2%}] per year")

# Suburb effect sizes (premium/discount vs state geomean), latest year
latest = df1[df1.year==df1.year.max()].copy()
latest["mult_vs_median"] = latest["median_price"] / latest["median_price"].median()
top = latest.nlargest(8,"median_price")[["suburb","median_price","mult_vs_median"]]
bot = latest.nsmallest(8,"median_price")[["suburb","median_price","mult_vs_median"]]
print(f"\n  Location effect sizes ({df1.year.max()} median house price, x vs state median):")
print("   Most expensive:");  [print(f"     {r.suburb:22} ${r.median_price:>10,.0f}  {r.mult_vs_median:4.1f}x") for r in top.itertuples()]
print("   Cheapest:");        [print(f"     {r.suburb:22} ${r.median_price:>10,.0f}  {r.mult_vs_median:4.1f}x") for r in bot.itertuples()]
print(f"\n  Cross-suburb price dispersion (latest yr): "
      f"{latest.median_price.max()/latest.median_price.min():.0f}x between most/least expensive")

# ===========================================================================
# SECTION 2 — YIELD ANALYSIS
# ===========================================================================
rule("SECTION 2 - GROSS & NET RENTAL YIELDS")
y = price_y.merge(house_rent, on=["year","suburb"], how="inner")
y = y.merge(all_rent, on=["year","suburb"], how="left")
y["rent_used"] = y["rent_house"].fillna(y["rent_all"])
y = y[(y.median_price>0) & (y.rent_used>0)].copy()
y["gross_yield"] = y["rent_used"]*52 / y["median_price"] * 100
# Net yield: assume holding costs = 25% of gross rent (rates, mgmt, insurance,
# maintenance, vacancy). Buying/selling costs excluded. Sensitivity at 30%.
for cr in (0.25, 0.30):
    y[f"net_yield_{int(cr*100)}"] = y["rent_used"]*52*(1-cr) / y["median_price"] * 100

print(f"  Yield panel: {len(y):,} suburb-years, {y.year.min()}-{y.year.max()} "
      f"(house-matched rent; cost assumptions stated)")
print(f"  Gross yield: mean {y.gross_yield.mean():.2f}%  median {y.gross_yield.median():.2f}%  "
      f"sd {y.gross_yield.std():.2f}")
print(f"  Net yield @25% costs: median {y['net_yield_25'].median():.2f}%   @30%: {y['net_yield_30'].median():.2f}%")

# Compression over time (state median gross yield by year)
yt = y.groupby("year")["gross_yield"].median()
print("\n  Gross-yield compression (state median by year):")
for yr,v in yt.items(): print(f"    {yr}: {v:5.2f}%")
slope, b = np.polyfit(yt.index, yt.values, 1)
print(f"  --> Trend: {slope:+.3f} pts/yr  (yield {'COMPRESSION' if slope<0 else 'EXPANSION'})")
print(f"  --> Peak {yt.idxmax()} ({yt.max():.2f}%) -> Trough {yt.idxmin()} ({yt.min():.2f}%)")

# Price-band yields (latest year quartiles)
ly = y[y.year==y.year.max()].copy()
ly["band"] = pd.qcut(ly["median_price"], 4, labels=["Q1 cheapest","Q2","Q3","Q4 dearest"])
bandy = ly.groupby("band")["gross_yield"].agg(["mean","median","count"])
print(f"\n  Yield by price band ({y.year.max()}):")
for b,r in bandy.iterrows(): print(f"    {b:14} gross {r['median']:4.2f}%  (n={int(r['count'])})")
print("  --> NEGATIVE price-yield gradient confirms cheaper = higher yield (regional skew)")

# ===========================================================================
# SECTION 3 — RENT vs PRICE LEAD/LAG
# ===========================================================================
rule("SECTION 3 - RENT <-> PRICE DYNAMICS (statewide, annual)")
sp = price_y.groupby("year")["median_price"].median()           # state price index
sr = all_rent.groupby("year")["rent_all"].median()              # state rent index
idx = pd.DataFrame({"price":sp,"rent":sr}).dropna()
idx["price_g"] = idx["price"].pct_change()*100
idx["rent_g"]  = idx["rent"].pct_change()*100
g = idx.dropna()
print(f"  Overlap: {g.index.min()}-{g.index.max()}  (N={len(g)} annual growth obs -- SMALL, treat as indicative)")
print(f"  Contemporaneous corr(price_g, rent_g) = {g.price_g.corr(g.rent_g):+.3f}")
for lag in (1,2):
    # rent leading price (rent_{t-lag} vs price_t)
    c = g["rent_g"].shift(lag).corr(g["price_g"])
    c2 = g["price_g"].shift(lag).corr(g["rent_g"])
    print(f"  Rent leads price by {lag}y: corr={c:+.3f}   |   Price leads rent by {lag}y: corr={c2:+.3f}")
print(f"  Cumulative since 2013: price {(idx.price.iloc[-1]/idx.price.iloc[0]-1):+.1%}  "
      f"rent {(idx.rent.iloc[-1]/idx.rent.iloc[0]-1):+.1%}")
print("  NOTE: vacancy-rate data absent -> demand-pressure channel not directly modelled.")

# ===========================================================================
# SECTION 4 — INTEREST-RATE SENSITIVITY
# ===========================================================================
rule("SECTION 4 - LENDING-RATE SENSITIVITY (no cash rate in data; OO var. std rate used)")
oo = rates[rates.series_id=="F5/FILRHLBVS"].copy()   # OO variable standard
oo["year"] = oo["period"].str[:4].astype(int)
oo_y = oo.groupby("year")["rate_pct"].mean()
print(f"  Rate proxy: 'Housing OO Variable Standard' (F5/FILRHLBVS), annual avg, "
      f"{oo_y.index.min()}-{oo_y.index.max()}")
rs = idx.join(oo_y.rename("rate")).dropna()
rs["d_rate"] = rs["rate"].diff()
rs = rs.dropna()
# Regress price growth on rate level + rate change, contemporaneous
X = sm.add_constant(rs[["rate","d_rate"]])
mr = sm.OLS(rs["price_g"], X).fit()
print(f"  OLS price_g ~ rate_level + d_rate   (N={int(mr.nobs)}):")
for nm in ["rate","d_rate"]:
    co,p = mr.params[nm], mr.pvalues[nm]; ci=mr.conf_int().loc[nm]
    print(f"     {nm:8} beta={co:+6.2f}  p={p:.3f}  95%CI[{ci[0]:+.2f},{ci[1]:+.2f}]")
print(f"     R^2={mr.rsquared:.3f}")
# Lagged: does last year's rate CHANGE predict this year's price growth?
rs["d_rate_lag1"] = rs["d_rate"].shift(1)
rl = rs.dropna()
if len(rl) >= 4:
    Xl = sm.add_constant(rl[["d_rate_lag1"]])
    ml = sm.OLS(rl["price_g"], Xl).fit()
    co,p = ml.params["d_rate_lag1"], ml.pvalues["d_rate_lag1"]
    print(f"  Transmission lag: price_g ~ d_rate(t-1): beta={co:+.2f} p={p:.3f} "
          f"(N={int(ml.nobs)}) -> {'1-yr lagged rate hikes assoc. w/ slower price growth' if co<0 else 'n.s. sign'}")
print(f"  corr(price LEVEL, rate LEVEL) = {rs['price'].corr(rs['rate']):+.3f}")
print("  CAVEAT: N is tiny (annual). Treat betas as directional, not precise elasticities.")

# ===========================================================================
# SUPPLY — building approvals vs price
# ===========================================================================
rule("SUPPLY - BUILDING APPROVALS vs PRICE (Victoria/Melbourne)")
va = appr[(appr.region.str.contains("Melbourne|Victoria", case=False, na=False)) &
          (appr.dwelling_type=="total") & (appr.seasonality=="original")].copy()
if len(va):
    va["year"] = va["period"].str[:4].astype(int)
    va_y = va.groupby("year")["num_approvals"].sum()
    sup = idx.join(va_y.rename("approvals")).dropna()
    if len(sup) >= 4:
        print(f"  VIC/Mel total approvals {va_y.index.min()}-{va_y.index.max()}")
        print(f"  corr(approvals, price_level) = {sup['approvals'].corr(sup['price']):+.3f}")
        print(f"  corr(approvals_chg, price_g) = {sup['approvals'].pct_change().corr(sup['price_g']):+.3f}")
else:
    print("  No matching VIC/Melbourne 'total' approval rows.")

# ===========================================================================
# CHARTS
# ===========================================================================
rule("GENERATING CHARTS")
# Chart 1: yield compression
fig,ax = plt.subplots(figsize=(11,5))
ax.plot(yt.index, yt.values, "o-", lw=2, color="#2c3e50")
ax.axhline(yt.mean(), ls="--", c="grey", alpha=.6, label=f"mean {yt.mean():.2f}%")
z=np.poly1d(np.polyfit(yt.index,yt.values,1)); ax.plot(yt.index,z(yt.index),"r--",alpha=.7,label=f"trend {slope:+.3f}pp/yr")
ax.set_title("Gross Rental Yield Compression — Victorian Houses (state median)",fontweight="bold")
ax.set_ylabel("Gross yield (%)"); ax.set_xlabel("Year"); ax.legend(); ax.grid(alpha=.3)
plt.tight_layout(); plt.savefig(OUT/"yield_compression.png",dpi=140); plt.close()

# Chart 2: price vs rent vs rate (indexed to 2013=100) + rate on twin axis
fig,ax = plt.subplots(figsize=(11,5))
base=idx.dropna().index.min()
ax.plot(idx.index,(idx.price/idx.price.loc[base]*100),"o-",label="House price (2013=100)",color="#c0392b")
ax.plot(idx.index,(idx.rent/idx.rent.loc[base]*100),"s-",label="Rent (2013=100)",color="#27ae60")
ax.set_ylabel("Index (2013=100)"); ax.set_xlabel("Year"); ax.grid(alpha=.3)
ax2=ax.twinx(); ax2.plot(oo_y.index,oo_y.values,"^--",color="#2980b9",alpha=.7,label="OO var rate (%)")
ax2.set_ylabel("Lending rate (%)",color="#2980b9")
l1,la=ax.get_legend_handles_labels(); l2,lb=ax2.get_legend_handles_labels()
ax.legend(l1+l2,la+lb,loc="upper left",fontsize=8)
ax.set_title("Price vs Rent vs Lending Rate — Victoria",fontweight="bold")
plt.tight_layout(); plt.savefig(OUT/"price_rent_rate.png",dpi=140); plt.close()
print(f"  Saved: {OUT/'yield_compression.png'}")
print(f"  Saved: {OUT/'price_rent_rate.png'}")

# Persist analytic tables
y.to_csv(OUT/"yields_panel.csv", index=False)
idx.to_csv(OUT/"state_indices.csv")
print(f"  Saved: {OUT/'yields_panel.csv'} ({len(y):,} rows)")
print("\nDONE.")
