"""Generate a ydata-profiling EDA report and drop it where the app's Data Profile tab finds it.

    python scripts/generate_profile.py                       # scraped listings (latest obs per MLS ref)
    python scripts/generate_profile.py --all-observations    # every listing observation
    python scripts/generate_profile.py path/to/train.parquet --name training_set
    python scripts/generate_profile.py path/to/rows.csv --full

SOURCE is `listings` (default) or a path to a .csv / .parquet / .ndjson / .jsonl / .json file.
`listings` reads the same source as the Listings explorer: LISTINGS_NDJSON_PATH if set,
otherwise BigQuery via Application Default Credentials. A path is profiled as-is.

Reports are written to reports/data_profile/<name>.html (override the folder with PROFILE_DIR).
By default the quick "minimal" profile is used; pass --full for correlations and pairwise
interactions, which is slow on wide tables.
"""
import argparse
import os
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

PROFILE_DIR = Path(os.environ.get("PROFILE_DIR") or ROOT / "reports" / "data_profile")

# Pure identifiers, hashes, URIs and free text: they add noise (and time) to a profile
# without telling you anything about the distribution of the data.
LISTING_DROP = [
    "record.record_id", "provenance.listing_url", "provenance.canonical_url",
    "provenance.snapshot_uri", "provenance.snapshot_sha256", "provenance.raw_payload_uri",
    "listing.title", "listing.description", "location.address_raw",
]


def load_listings(all_observations: bool) -> pd.DataFrame:
    from app import listings  # imported lazily so a plain-file run needs no backend deps

    path = os.environ.get("LISTINGS_NDJSON_PATH")
    rows = listings._load_ndjson(path) if path else listings._load_bigquery()
    if not all_observations:
        rows = listings._latest_per_ref(rows)

    df = pd.json_normalize(rows, sep=".")
    # Lists (image lists, parse errors, feature tags) profile badly; keep their lengths instead.
    for col in [c for c in df.columns if df[c].map(lambda v: isinstance(v, list)).any()]:
        df[f"{col}_count"] = df[col].map(lambda v: len(v) if isinstance(v, list) else 0)
        df = df.drop(columns=col)
    df = df.drop(columns=[c for c in LISTING_DROP if c in df.columns])
    if "provenance.fetched_at" in df.columns:
        df["provenance.fetched_at"] = pd.to_datetime(df["provenance.fetched_at"], errors="coerce", utc=True)
    return df


def load_file(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(path)
    if suffix == ".parquet":
        return pd.read_parquet(path)
    if suffix in (".ndjson", ".jsonl"):
        return pd.json_normalize(pd.read_json(path, lines=True).to_dict("records"), sep=".")
    if suffix == ".json":
        return pd.read_json(path)
    raise SystemExit(f"Unsupported file type {suffix!r}; use .csv, .parquet, .ndjson, .jsonl or .json")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", nargs="?", default="listings")
    ap.add_argument("--name", help="report name (default: 'listings' or the file's stem)")
    ap.add_argument("--all-observations", action="store_true",
                    help="listings only: keep every observation instead of the latest per MLS ref")
    ap.add_argument("--full", action="store_true", help="full profile with correlations and interactions")
    ap.add_argument("--sample", type=int, metavar="N", help="profile a random sample of N rows")
    args = ap.parse_args()

    if args.source == "listings":
        df, name = load_listings(args.all_observations), args.name or "listings"
    else:
        p = Path(args.source)
        if not p.is_file():
            raise SystemExit(f"No such file: {p}")
        df, name = load_file(p), args.name or p.stem
    if not name.replace("-", "").replace("_", "").replace(".", "").isalnum():
        raise SystemExit("--name may only contain letters, digits, '-', '_' and '.'")
    if args.sample and args.sample < len(df):
        df = df.sample(args.sample, random_state=0)

    from ydata_profiling import ProfileReport

    print(f"Profiling {len(df):,} rows x {df.shape[1]} columns ({'full' if args.full else 'minimal'} mode)...")
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    out = PROFILE_DIR / f"{name}.html"
    ProfileReport(df, title=f"TRIDENT-Val profile: {name}", minimal=not args.full).to_file(out)
    print(f"Wrote {out} ({out.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
