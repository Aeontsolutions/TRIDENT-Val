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

One command does the setup below (venv, dependencies, `npm install`, demo users) and is safe to re-run:
`python scripts/dev_setup.py` (add `--dump-listings` to also save the BigQuery table locally, see Listings source).
Working on this with Claude Code? Read `CLAUDE.md` first.

Manual steps:

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
- **Record inspector**: raw vs parsed fields side by side, provenance, quality flags, images, and a
  Model estimate section (below).
- **Bad-parse flags**: flag a raw-vs-parsed field (wrong value / not parsed / wrong unit / ...), list
  and filter flags, export them as CSV.
- **Data Profile** (Admin): embeds ydata-profiling reports; see below.

## Model estimate

The record inspector has a **Model estimate** section: pick a champion and a challenger model version
and press **Score this listing** to see what the registered listing-price model (`avm_listing_price`)
would say about that listing, next to the asking price. It is read-only: it reads the model registry,
BigQuery and the stored listing page, and writes nothing to BigQuery, GCS or the registry (it caches the page and
the model artifact locally).

What it shows: the point estimate with its 90% range and the gap to the asking price for each slot, the
exact inputs fed to the model (bedrooms and bathrooms above 8 are flagged as outliers), and warnings.

- **Estimates are asking prices, in JMD.** The model is trained on asking prices, not sale prices.
- **Not an out-of-sample test.** "Probably in training" means the listing's `listing_clean` row has
  `trainable = true`. The locked test split is not reconstructed, so a trainable listing may have been held
  back in that split and never trained on. A listing that is not marked trainable is in neither the training
  set nor the test split.
- **Listings in `listing_clean`** are scored from the clean row (the observation held there, which may not be
  the one on screen; a warning says so). The listing's stored page is read for the page fields (pool, views,
  gated security, lot size and so on), as in training. **Listings not in the clean table** are scored from the
  raw explorer fields with a warning.
- **Not scored:** listings that are not for sale, or whose property type is not house, apartment or townhouse.
- **Slots.** Either slot takes any registered version, or an alias. The challenger slot defaults to the
  registry alias `challenger`; none exists yet (only `champion` -> version 1), so by default it shows
  "no challenger registered". Set the alias on a version in the registry, or pick a version number.
- **It is slow.** Every click spawns the scorer. The first click for a model version downloads a ~44 MB
  artifact. A click takes roughly 15-40 s; a listing that is not in the clean table can take about a minute,
  because its page is downloaded and more BigQuery queries run. If the scorer exceeds `MODEL_CHECK_TIMEOUT_S`
  it is killed and the slots show an error.
- **Known limitation: no model is kept in memory between clicks.** Each click starts a fresh scorer process,
  so every click reloads the model (the artifact is cached on disk after the first download, but loading it
  still takes time).

### Requirements

- `uv` on PATH. The scorer runs under the `ats_avm` monorepo environment
  (`uv run --package avm_model --extra prototype`), because `avm_model` pins `scikit-learn==1.9.1`. It is a
  separate process on purpose: do not install `avm_model` or `mlflow` in the explorer's venv.
- The `ats_avm` monorepo checkout. By default it is two folders above `tools/trident-val` (the `ats_avm`
  root); set `MODEL_CHECK_ATS_AVM_ROOT` otherwise. It must contain `packages/avm_model`.
- The MLflow proxy running in another terminal (the registry is private):
  `gcloud run services proxy mlflow --project ats-avm-jam --region us-central1 --port 5000`.
- `gcloud auth application-default login` (BigQuery reads).
- `gcloud` on PATH: the stored listing page is read with `gcloud storage cat`.

| Env var | Default | Meaning |
|---|---|---|
| `MLFLOW_TRACKING_URI` | `http://localhost:5000` | The MLflow registry, normally the proxy above. Passed on to the scorer. |
| `MODEL_CHECK_ATS_AVM_ROOT` | the `ats_avm` root, two folders above `tools/trident-val` | Monorepo checkout the scorer runs in. |
| `MODEL_CHECK_EXTRA` | `prototype` | `avm_model` extra the scorer installs. Set `tabpfn` to score the TabPFN challenger. That needs PyTorch, a `tabpfn` extra in the `MODEL_CHECK_ATS_AVM_ROOT` checkout (`dev_elroy` has it), and `TABPFN_TOKEN` in the backend's environment. The first run downloads the weights, so raise `MODEL_CHECK_TIMEOUT_S`. |
| `MODEL_CHECK_TIMEOUT_S` | `180` | Seconds before a scorer run is killed (with its child processes) and reported as an error. |
| `MODEL_CHECK_PAGES_DIR` | `<explorer>/.cache/pages` | Cache of stored listing pages read from GCS (git-ignored). |

API: `GET /api/v1/model-check/versions` lists registered versions and aliases;
`POST /api/v1/model-check/score` takes `{"record_id", "champion", "challenger"}`.

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
  model_check.py     Model estimate API (/api/v1/model-check): registry reads, runs the scorer subprocess
  model_limits.py    input plausibility limits (bedrooms, bathrooms) and the flags shown in the UI
  inference.py       kept from the Ames demo; unused by the app (model_check.py replaces it)
  auth.py db.py models.py logging_config.py
backend/scorer/      score_listing.py + input_row.py: run under the monorepo env, not this venv (see Model estimate)
backend/tests/       listings and model-check tests, and an NDJSON fixture
frontend/src/        React + Tailwind views (Listings*, DataProfile, Login)
scripts/             dev_setup.py, seed_users.py, generate_profile.py
```
