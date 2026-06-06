# vic-housing-data

Production-grade Python pipeline for Victorian residential housing data, drawn
from official Australian government and ASX sources. Built for institutional
finance and equity research workflows.

---

## Data Sources

| Connector | Source | Cadence | What you get |
|-----------|--------|---------|--------------|
| `vgv` | [data.vic.gov.au](https://discover.data.vic.gov.au/) — Valuer-General Victoria | Quarterly | Median house / unit / land sale prices by suburb & LGA |
| `rental` | [data.vic.gov.au](https://discover.data.vic.gov.au/) — DFFH Rental Report (RTBA) | Quarterly | Median weekly rent by suburb & LGA × bedroom count |
| `abs` | [ABS Data API](https://data.api.abs.gov.au/) — Building Approvals 8731.0 | Monthly | Dwelling approvals by region & type (number and value) |
| `rba` | [RBA Statistical Tables](https://www.rba.gov.au/statistics/tables/) — F5, F6, F7 | Monthly | Lending rates: owner-occupier vs investor, P&I vs IO |
| `asx` | ASX public announcements feed | Near real-time | Filings for MGR, SGP, LLC, DHG, GMG, REA, VCX, CQR, CLW, HMC |

---

## Architecture

```
vic_housing/
├── core.py        # HTTP session, SQLite schema, disk cache, logging
├── vgv.py         # Valuer-General Victoria — median sale prices
├── rental.py      # DFFH — median rents (RTBA bond data)
├── abs.py         # ABS — building approvals (SDMX API + XLSX fallback)
├── rba.py         # RBA — lending rate series F5/F6/F7
├── asx.py         # ASX — property-sector announcements
├── exports.py     # CSV + multi-sheet Excel dashboard generator
└── pipeline.py    # CLI orchestrator
```

### Database schema (SQLite)

Five normalised tables, all idempotent (INSERT OR IGNORE on UNIQUE constraints):

| Table | Key columns |
|-------|-------------|
| `sales_medians` | period, suburb, lga, dwelling_type → median_price, num_sales |
| `rental_medians` | period, suburb, lga, dwelling_type → median_rent |
| `building_approvals` | period, region, dwelling_type, seasonality → num_approvals, value_000 |
| `lending_rates` | period, series_id → rate_pct, series_label |
| `asx_announcements` | ticker, announced_at, headline → url |

Plus `pipeline_runs` for observability.

### Excel dashboard

`exports/vic_housing_dashboard.xlsx` contains:
- One sheet per table
- A derived **Yields_Calc** sheet — joins `sales_medians` (houses) to
  `rental_medians` (all dwellings) and computes gross yield % by suburb/period:
  `gross_yield = (median_weekly_rent × 52) / median_price × 100`

---

## Installation

```bash
git clone https://github.com/yourname/vic-housing-data.git
cd vic-housing-data
pip install -r requirements.txt
```

Python ≥ 3.10 required.

---

## Usage

```bash
# Run the full pipeline (all connectors + export)
python -m vic_housing.pipeline

# Run only the macro data connectors
python -m vic_housing.pipeline --only rba abs

# Skip the ASX feed
python -m vic_housing.pipeline --skip asx

# Re-export from existing database without fetching
python -m vic_housing.pipeline --export-only

# Fetch without exporting
python -m vic_housing.pipeline --no-export
```

---

## Configuration

| Environment variable | Default | Purpose |
|----------------------|---------|---------|
| `VIC_CACHE_TTL` | `86400` (24 h) | HTTP cache TTL in seconds |

Set `VIC_CACHE_TTL=0` to disable caching (forces fresh fetches every run).

---

## Design notes

**Idempotent by default.** Every connector uses INSERT OR IGNORE against
UNIQUE constraints, so re-running never duplicates data.

**Defensive parsing.** VGV and DFFH spreadsheet layouts change periodically.
Parsers use heuristics (scan for header keywords, detect dwelling type from
sheet/filename) rather than hard-coded cell coordinates. When a sheet can't
be parsed, it's skipped with a warning rather than crashing the pipeline.

**Isolated connectors.** A failure in one connector (e.g. a 403 on the ABS
API) does not kill the rest of the pipeline. All failures are logged to
`logs/pipeline.log` and the `pipeline_runs` table.

**Rate limiting.** The ASX connector waits 1 second between ticker requests.
The requests Session applies exponential backoff on 429/5xx responses.

---

## AFSL / legal note

All data sources are publicly available and free to use for research purposes.
This pipeline does not scrape Domain, REA Group, or any platform whose Terms
of Service prohibit automated access. Do not redistribute the raw data
commercially without reviewing each source's licence terms.

---

## Relevant ASX tickers

| Ticker | Company | Relevance |
|--------|---------|-----------|
| MGR | Mirvac Group | Residential developer + commercial REIT |
| SGP | Stockland | Master-planned communities, logistics |
| LLC | Lendlease | Construction + urban regeneration |
| DHG | Domain Holdings | Residential property listings platform |
| GMG | Goodman Group | Industrial / logistics REIT (proxy for land) |
| REA | REA Group | Property listings (realestate.com.au) |
| VCX | Vicinity Centres | Retail REIT |
| CQR | Charter Hall Retail REIT | Convenience retail |
| CLW | Charter Hall Long WALE REIT | Long-lease diversified |
| HMC | Home Consortium | Large-format retail + HomeCo Daily Needs |

---

*Built as part of an independent equity research data infrastructure project.*
