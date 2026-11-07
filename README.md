# TRIDENT-Val: listings explorer (avm-explorer branch)

A local tool for sanity-checking the ATS AVM data pipeline: a FastAPI backend and a React
workbench over the scraped Jamaican listings (`listings_raw.listing_observation_raw`), plus
ydata-profiling reports for any tabular dataset you point it at.

This branch is a trimmed fork of TRIDENT-Val's `main`. The Ames-based demo (portfolio views,
champion/challenger governance, revaluation cycles, PDF reports, AI copilot, Terraform and
deploy workflow) lives on `main`; nothing was removed from there.

**Local use only.** It ships fixed demo logins (below). Do not deploy it or expose it beyond
localhost.

## Quickstart

```bash
python -m venv .venv            # Python 3.12
.venv/Scripts/pip install -r backend/requirements.txt     # macOS/Linux: .venv/bin/pip
.venv/Scripts/python scripts/seed_users.py                 # creates ./trident.db and the demo logins

# terminal 1 (backend, http://localhost:8000)
cd backend && ../.venv/Scripts/python -m uvicorn app.main:app --port 8000 --reload

# terminal 2 (frontend, http://localhost:5173)
cd frontend && npm install && npm run dev
```

Log in as `admin` / `admin123` (also `underwriter` / `underwriter123` and `viewer` / `viewer123`).
`seed_users.py` only creates missing tables and users; it never drops anything, so reviewer flags
survive a re-run.

## Listings source

| Env var | Default | Meaning |
|---|---|---|
| `LISTINGS_NDJSON_PATH` | unset | Read local NDJSON (a file, or a directory of `*.ndjson`) instead of BigQuery. Fastest for development: BigQuery loads all rows with a plain `SELECT *` (about 75 s for ~9,000 rows) and every backend reload repeats it. |
| `LISTINGS_BQ_PROJECT` | `ats-avm-jam` | Billing/job project for the query (must not be the gcloud default). |
| `LISTINGS_BQ_TABLE` | `ats-avm-jam.listings_raw.listing_observation_raw` | Table to read. |
| `LISTINGS_CACHE_TTL` | `300` | Seconds the loaded rows stay cached in memory. |
| `DATABASE_URL` | `sqlite:///./trident.db` | App DB (users, bad-parse flags, logs). |

BigQuery uses Application Default Credentials (`gcloud auth application-default login`). To work
offline, pull the raw batches with
`gcloud storage cp "gs://ats-avm-jam-listings-raw/batches/cbjamaica/*.ndjson" ./listings/` and set
`LISTINGS_NDJSON_PATH=./listings` (rows are de-duplicated by `record_id`).

## What's in the explorer

The listings table is read-only; the one write path is reviewer bad-parse flags, stored in the app DB.

- **Quality overview**: fill rate per field, parser gaps (raw text present but parsed value missing),
  parse status / currency / geo precision / parish / property-type counts, rows per scrape run.
- **Listings grid**: paginated cards with filters, search and sort.
- **Distributions**: log-binned histograms of price, floor area, lot size and price per area, with IQR
  outlier fences per currency/unit facet.
- **Map**: pins by property type or price quartile, plus suspicious geometry (pins outside Jamaica,
  clusters of different MLS refs on identical coordinates).
- **Run comparison**: how much the same listing recurs across scrape runs, and a field-level diff
  between any two runs.
- **Record inspector**: raw vs parsed fields side by side, provenance, quality flags, images.
- **Bad-parse flags**: flag a raw-vs-parsed field (wrong value / not parsed / wrong unit / ...), list
  and filter flags, export them as CSV.
- **Data Profile** (Admin): embeds ydata-profiling reports; see below.

## Data profiles

```bash
python scripts/generate_profile.py                          # scraped listings, latest observation per MLS ref
python scripts/generate_profile.py --all-observations       # every observation
python scripts/generate_profile.py path/to/train.parquet --name training_set
python scripts/generate_profile.py path/to/rows.csv --full  # correlations and interactions (slow on wide data)
```

Accepts `.csv`, `.parquet`, `.ndjson`, `.jsonl` and `.json`. Reports are written to
`reports/data_profile/<name>.html` (git-ignored; override with `PROFILE_DIR`) and show up in the
Data Profile tab after a reload.

## Tests

```bash
cd backend && ../.venv/Scripts/python -m pytest tests
```

## Layout

```
backend/app/
  main.py            app, login/session, logs, data-profile routes
  listings.py        read-only listings explorer API
  listing_flags.py   bad-parse flags (the only write path)
  inference.py       kept from the Ames demo as a starting point for a model-check adapter; unused by the app
  auth.py db.py models.py logging_config.py
backend/tests/       listings tests and an NDJSON fixture
frontend/src/        React + Tailwind views (Listings*, DataProfile, Login)
scripts/             seed_users.py, generate_profile.py
```
