# TRIDENT-Val (avm-explorer branch): notes for Claude Code sessions

A local tool for sanity-checking the ATS AVM listings data pipeline: a read-only listings explorer
(FastAPI + React) and ydata-profiling reports. It is checked into the `ats_avm` repo as a git
submodule at `tools/trident-val`. It is **not** part of the AVM platform: nothing in `ats_avm`
imports it, and it has its own dependencies, tests and README. Read `README.md` for the feature
list and env vars; this file covers how to work on it.

## Branches (read before committing)
- **`avm-explorer`** is the working branch and the one `ats_avm` tracks. Commit and push here.
- **`main`** is the original Ames-based demo (portfolio views, champion/challenger governance, PDF
  reports, AI copilot, Terraform). Never push to it and never merge `avm-explorer` into it: the
  trim deleted most of it on purpose. To borrow something from the old app, read it with
  `git fetch --depth 1 origin main && git show origin/main:<path>`.
- Inside an `ats_avm` checkout the submodule is on a **detached HEAD** after
  `git submodule update --init`. Run `git switch avm-explorer` before editing, or your commits can
  be lost.

## Changing it: two commits, in two repos
1. In `tools/trident-val`: `git switch avm-explorer`, make the change, commit, `git push origin avm-explorer`.
2. In the `ats_avm` repo root: `git add tools/trident-val` and commit ("Bump trident-val to <sha>").
   Skipping this leaves `ats_avm` (and `dev_elroy`) pinned to the old version.

## Setup (per worktree; none of it is tracked)
```bash
python scripts/dev_setup.py                  # .venv, backend deps, npm install, trident.db + demo users
python scripts/dev_setup.py --dump-listings  # also save the BigQuery table to ./listings/ (~75 s)
```
The dump matters for development: the backend loads all listings with a plain `SELECT *` (~75 s),
and every `--reload` restart repeats it. With the dump, run the backend with
`LISTINGS_NDJSON_PATH=./listings` and reloads are instant. BigQuery needs Application Default
Credentials (`gcloud auth application-default login`); the project is `ats-avm-jam`.

## Run, test, verify
```bash
# backend (from backend/); the timeout flag stops --reload hanging on open browser connections
../.venv/Scripts/python -m uvicorn app.main:app --port 8000 --reload --timeout-graceful-shutdown 2
# frontend (from frontend/); if the backend is on another port: API_URL=http://localhost:8001 npm run dev
npm run dev

cd backend && ../.venv/Scripts/python -m pytest tests     # 29 tests; run after backend changes
cd frontend && npx vite build --outDir <somewhere outside the repo>    # catches broken imports
uvx ruff check --select F401,F821,F841 backend/app scripts            # undefined and unused names
```
On macOS/Linux use `.venv/bin/python`. Demo logins are in the README (local use only).

## Rules and conventions
- **Local use only.** The demo logins are fixed and public. Do not deploy this, add Terraform, or
  expose it beyond localhost.
- **The listings table is read-only.** The only write path is reviewer bad-parse flags in the app DB
  (`listing_flags`). Never write to BigQuery or GCS from here.
- **`scripts/seed_users.py` must stay non-destructive.** It only creates missing tables and users.
  Do not reintroduce `drop_all`; it would wipe reviewers' flags.
- **Do not add Ames, portfolio or governance code back.** If a model-check view is wanted, build it
  against the real `avm_model` package (a cascade with conformal intervals and tiering), not the
  old single-estimator `log1p(price)` shape.
- **`backend/app/inference.py` is unused.** It is kept only as a starting point for a model-check
  adapter. Nothing imports it; do not wire it in unchanged.
- **Data profiles** are written by `scripts/generate_profile.py` to `reports/data_profile/` and shown
  in the Admin-only Data Profile tab. They are static snapshots. See the README for usage.
- **Line endings:** files are CRLF in the working tree on Windows (autocrlf). If you edit with
  scripts, keep each file's existing line endings.

## Known gaps
- The Listings coverage banner (`COVERAGE_NOTE` in `backend/app/listings.py`) says "~540 of ~7,600
  listings", but the table now holds ~9,000 observations. It has not been updated.
- On Windows, a killed uvicorn can leave its port stuck in LISTEN. Use another port and point Vite at
  it with `API_URL` rather than fighting it.
