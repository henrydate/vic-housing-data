# vic-housing-data

A reproducible Python pipeline that assembles a Victorian housing-market dataset
from **free, official sources** (Valuer-General Victoria, DFFH, ABS, RBA, ASX),
stores it in SQLite, and ships a quantitative analysis layer — rental-yield
decomposition, cash-rate sensitivity, and an interstate **difference-in-differences**
on Victorian housing policy.

Built end-to-end: data engineering (resilient connectors) → storage → econometric analysis → reporting.

---

## Headline findings

Reproduced live in `vic_housing_notebook.ipynb` and `analysis/`:

- **Location dominates price.** A log-price model gains **+83 points of R² (0.12 → 0.95)** when suburb fixed effects are added — location explains ~7× more variance than the entire time trend. Suburb price multiples span **31×** (Toorak 5.6× state median → regional towns 0.2×).
- **Yield compression, then reversal.** State-median gross house yield fell **3.46% (2013) → 2.33% (2021)**, then re-rated up to **3.07% (2025)** as the 2022–24 price dip met continued rent growth. Strong negative price–yield gradient: cheapest quartile **4.4%** vs dearest **1.9%**.
- **Rate sensitivity (cash rate).** Melbourne house-price *level* vs cash-rate *level* correlates **−0.91** over the long sample; a +1pp hike maps to roughly **−3% to −5%** on prices with a ~1-year lag.
- **Policy effect is a null at the aggregate.** A Melbourne-vs-Sydney difference-in-differences around the **2024 VIC land-tax expansion** returns only **−1 to −3 pts** — but placebo quarters swing **±10–18 pts**, so any land-tax price effect is **buried in city-cycle noise**. Policy bites at the *segment* level, not the aggregate.
- **Structural shift.** Greater Melbourne's median established house (**$875k, 2025-Q4**) is now **cheaper than Brisbane, Perth and Adelaide** — from 2nd-dearest capital historically to 5th.

> Honest scope: this repo holds **aggregated median** data, not transaction-level
> records. Hedonic drivers (beds/baths, land size, school zones), auction
> clearance, days-on-market and vacancy rates are **not modelled — they are not in
> any free source at this granularity.** Every gap is flagged in the analysis
> rather than faked.

---

## Data sources (all free, all official)

| Connector | Source | Cadence | Coverage |
|-----------|--------|---------|----------|
| `vgv` | [data.vic.gov.au](https://discover.data.vic.gov.au/) — Valuer-General Victoria | Quarterly + annual | Median house/unit/land prices by suburb, 2013–2025 |
| `rental` | [dffh.vic.gov.au](https://www.dffh.vic.gov.au/) — Rental Report (RTBA bond data) | Quarterly | Median weekly rent by suburb × bedroom type, 2000–2025 |
| `abs` | [ABS Data API](https://data.api.abs.gov.au/) — Building Approvals (BA_GCCSA) | Monthly | Dwelling approvals by region & type |
| `rba` | [RBA tables](https://www.rba.gov.au/statistics/tables/) — F5, F6 | Monthly | Housing lending rates (OO vs investor, P&I vs IO) |
| `cashrate` | RBA table **F1.1** — Cash Rate Target | Monthly | Policy cash rate, 1990–present |
| `capitals` | ABS Data API — **RES_DWELL** | Quarterly | Median house/unit price for **every capital city** (interstate control) |
| `asx` | ASX (MarkitDigital backend) | Recent | Property-sector filings (MGR, SGP, LLC, GMG, REA, VCX, CQR, CLW, HMC, DXS) |

### A note on resilience (the interesting engineering)

- **`land.vic.gov.au` blocks bots** (403 on every direct request, even with browser
  headers). The `vgv` connector works around this by resolving each file through the
  **Wayback Machine** (CDX API → `if_` raw-content URLs), so the pipeline stays free
  and unattended. Files too recent to be archived can be dropped into `manual_imports/`.
- **DFFH rental** links are HTML pages that redirect to the real `.xlsx`; the connector
  follows the redirect and detects the spreadsheet by magic bytes.
- **VGV spreadsheets ship in three different layouts** across years (wide quarterly,
  split-header quarterly, annual time-series) — the parser auto-detects and handles all three.
- **CKAN `package_search`** (not hard-coded dataset IDs) so a dataset rename on the
  portal doesn't silently break the connector.

---

## Architecture

```
vic_housing/
├── core.py        # HTTP session (browser UA), SQLite schema, disk cache, logging
├── vgv.py         # Valuer-General — median sale prices (+ Wayback fallback, 3 layouts)
├── rental.py      # DFFH — median rents (page-redirect resolver, cumulative file)
├── abs.py         # ABS — building approvals (SDMX-JSON, region-filtered)
├── rba.py         # RBA — lending rate series F5/F6 (XLSX, dynamic header detection)
├── cashrate.py    # RBA — Cash Rate Target (F1.1)
├── capitals.py    # ABS — interstate capital-city median prices (RES_DWELL)
├── asx.py         # ASX — property-sector announcements (MarkitDigital)
├── exports.py     # CSV + multi-sheet Excel dashboard
└── pipeline.py    # CLI orchestrator (isolated, idempotent, logged)

analysis/
├── market_analysis.py      # variance decomposition, yields, rent–price lead/lag, supply
├── enhanced_analysis.py    # cash-rate sensitivity + interstate difference-in-differences
└── policy_event_study.py   # interrupted-time-series around VIC/federal policy dates
```

### Database (SQLite, 7 tables, all idempotent)

| Table | Key columns → values |
|-------|----------------------|
| `sales_medians` | period, suburb, lga, dwelling_type → median_price, num_sales |
| `rental_medians` | period, suburb, lga, dwelling_type → median_rent |
| `building_approvals` | period, region, dwelling_type, seasonality → num_approvals |
| `lending_rates` | period, series_id → rate_pct, series_label |
| `cash_rate` | period → rate_pct |
| `capital_prices` | period, region, measure → value |
| `asx_announcements` | ticker, announced_at, headline → url |

Plus `pipeline_runs` for observability. Every connector uses `INSERT OR IGNORE`
on UNIQUE constraints, so re-running never duplicates rows.

---

## Quick start

```bash
git clone https://github.com/<you>/vic-housing-data.git
cd vic-housing-data/vic_housing_pipeline/vic_housing
pip install -r requirements.txt          # Python >= 3.10

# Run the full pipeline (all connectors + export)
python -m vic_housing.pipeline

# Subsets
python -m vic_housing.pipeline --only rba abs cashrate capitals
python -m vic_housing.pipeline --skip asx
python -m vic_housing.pipeline --export-only      # re-export, no fetching

# Run the analysis
python analysis/market_analysis.py
python analysis/enhanced_analysis.py

# Or explore interactively
jupyter notebook vic_housing_notebook.ipynb
```

### Outputs

- `vic_housing.db` — query directly with pandas or DB Browser
- `exports/*.csv` — one per table
- `exports/vic_housing_dashboard.xlsx` — multi-sheet workbook with a derived yields tab
- `exports/analysis/*.png` — yield compression, price/rent/rate, Melbourne-vs-capitals DiD

---

## Configuration

| Env var | Default | Purpose |
|---------|---------|---------|
| `VIC_CACHE_TTL` | `86400` | HTTP cache TTL (seconds); set `0` to force fresh fetches |
| `RBA_INCLUDE_F7` | `false` | Include F7 *business* lending rates (off by default — not housing) |

---

## Design notes

- **Isolated connectors** — one connector failing (e.g. an ABS 503) never kills the run; failures are logged to `logs/pipeline.log` and `pipeline_runs`.
- **Defensive parsing** — header-keyword scanning and magic-byte detection rather than hard-coded cell coordinates, because government spreadsheet layouts drift.
- **Honest analysis** — where the data can't support a requested model (hedonics, causal policy attribution), the code says so explicitly instead of producing confident nonsense. The policy DiD is reported as a *null*, which is the correct finding.

## Legal / data licence

All sources are publicly available and free for research use. This pipeline does
**not** scrape Domain, REA, or any platform whose Terms of Service prohibit
automated access; the `land.vic.gov.au` files are retrieved via the public
Internet Archive. Review each source's licence before any commercial redistribution.

---

*Independent data-infrastructure & quantitative-analysis project.*
