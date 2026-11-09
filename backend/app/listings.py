"""Read-only explorer over scraped property listings (cbjamaica crawl) for data validation.

Source of truth is the BigQuery table written by the listings scraper
(`ats-avm-jam.listings_raw.listing_observation_raw`, one row per listing observation).
For offline / SQLite-mode runs the same records can be read from local NDJSON
(`LISTINGS_NDJSON_PATH`, a file or a directory of *.ndjson files — e.g. the batches under
gs://ats-avm-jam-listings-raw/batches/cbjamaica/).

The whole table is a few hundred rows, so it is loaded once into memory (TTL cache) and every
endpoint is computed in Python. That keeps one code path for both sources and makes the
router trivially testable with a fixture file. Nothing here writes anywhere: the only BigQuery
call is a plain SELECT, and there are no POST/PATCH/DELETE routes.

Config (env):
  LISTINGS_NDJSON_PATH   if set, read local NDJSON instead of BigQuery
  LISTINGS_BQ_PROJECT    billing/job project (default ats-avm-jam — NOT the gcloud default)
  LISTINGS_BQ_TABLE      fully-qualified table (default ats-avm-jam.listings_raw.listing_observation_raw)
  LISTINGS_CACHE_TTL     seconds to keep loaded rows (default 300)
"""
import datetime as dt
import decimal
import json
import math
import os
import re
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger

from .auth import get_current_user

router = APIRouter(prefix="/api/v1/listings", tags=["listings"],
                   dependencies=[Depends(get_current_user)])

DEFAULT_BQ_PROJECT = "ats-avm-jam"
DEFAULT_BQ_TABLE = "ats-avm-jam.listings_raw.listing_observation_raw"
_TABLE_RE = re.compile(r"^[A-Za-z0-9_.\-:]+$")

# Shown on every listings view; also returned by the API so the contract lives in one place.
COVERAGE_NOTE = (
    "Single-site crawl (cbjamaica.com) of the top of the market only: listings were collected "
    "price-descending and the site stops serving pages past ~50, so this is ~540 of ~7,600 "
    "listings. Prices are asking prices, not sale prices. Do not read these distributions as "
    "the whole Jamaican market."
)


# ---------- loading ----------

def _jsonable(v: Any) -> Any:
    """Normalise BigQuery client values into plain JSON types (matches NDJSON shape)."""
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, bytes):
        return v.hex()
    return v


def _load_ndjson(path: str) -> list[dict]:
    p = Path(path)
    files = sorted(p.glob("*.ndjson")) if p.is_dir() else [p]
    if not files or not all(f.exists() for f in files):
        raise FileNotFoundError(f"No NDJSON found at {path}")
    seen: dict[str, dict] = {}
    for f in files:
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                seen[rec["record"]["record_id"]] = rec
    return list(seen.values())


def _load_bigquery() -> list[dict]:
    from google.cloud import bigquery  # imported lazily: offline mode must not need it

    table = os.environ.get("LISTINGS_BQ_TABLE", DEFAULT_BQ_TABLE)
    if not _TABLE_RE.match(table):
        raise ValueError(f"Invalid LISTINGS_BQ_TABLE: {table!r}")
    project = os.environ.get("LISTINGS_BQ_PROJECT", DEFAULT_BQ_PROJECT)
    client = bigquery.Client(project=project)
    rows = client.query(f"SELECT * FROM `{table}`").result()
    return [_jsonable(dict(r.items())) for r in rows]


_cache: dict = {"rows": None, "at": 0.0, "source": None}
_lock = threading.Lock()


def clear_cache() -> None:
    with _lock:
        _cache.update(rows=None, at=0.0, source=None)


def _rows() -> list[dict]:
    ttl = float(os.environ.get("LISTINGS_CACHE_TTL", "300"))
    with _lock:
        if _cache["rows"] is not None and time.time() - _cache["at"] < ttl:
            return _cache["rows"]
        path = os.environ.get("LISTINGS_NDJSON_PATH")
        try:
            rows = _load_ndjson(path) if path else _load_bigquery()
        except Exception as e:  # surfaced to the UI as a 503 with the reason
            logger.error("Listings load failed: {e}", e=e)
            raise HTTPException(503, f"Listings source unavailable: {e}")
        _cache.update(rows=rows, at=time.time(),
                      source=f"ndjson:{path}" if path else "bigquery")
        logger.info("Loaded {n} listings from {s}", n=len(rows), s=_cache["source"])
        return rows


# ---------- helpers ----------

def _g(rec: dict, section: str, key: str) -> Any:
    return (rec.get(section) or {}).get(key)


def _present(v: Any) -> bool:
    return v is not None and v != "" and v != []


def _label(v: Any) -> str:
    return "(missing)" if not _present(v) else str(v)


def _counts(rows: list[dict], getter) -> list[dict]:
    c = Counter(_label(getter(r)) for r in rows)
    return [{"value": k, "count": n} for k, n in c.most_common()]


def _date_of(ts: Any) -> Optional[str]:
    return str(ts)[:10] if ts else None


# (label, section, key) — fill rate is reported for each
FILL_FIELDS = [
    ("price.amount", "price", "amount"),
    ("price.currency", "price", "currency"),
    ("listing.title", "listing", "title"),
    ("listing.description", "listing", "description"),
    ("listing.listing_type_raw", "listing", "listing_type_raw"),
    ("listing.listing_status_raw", "listing", "listing_status_raw"),
    ("listing.listed_at", "listing", "listed_at"),
    ("location.community", "location", "community"),
    ("location.parish_raw", "location", "parish_raw"),
    ("location.latitude", "location", "latitude"),
    ("location.longitude", "location", "longitude"),
    ("attributes.property_type_raw", "attributes", "property_type_raw"),
    ("attributes.bedrooms", "attributes", "bedrooms"),
    ("attributes.bathrooms", "attributes", "bathrooms"),
    ("attributes.floor_area", "attributes", "floor_area"),
    ("attributes.lot_size", "attributes", "lot_size"),
    ("attributes.year_built_raw", "attributes", "year_built_raw"),
    ("attributes.condition_raw", "attributes", "condition_raw"),
    ("attributes.features_raw", "attributes", "features_raw"),
    ("identifiers.mls_id_raw", "identifiers", "mls_id_raw"),
    ("agent.agency_name_raw", "agent", "agency_name_raw"),
    ("agent.agent_name_raw", "agent", "agent_name_raw"),
]

# raw string / parsed value pairs shown side by side. parsed=None means "no parsed field exists".
COMPARE_FIELDS = [
    ("Price", ("price", "amount_raw"), ("price", "amount"), ("price", "currency")),
    ("Currency", ("price", "currency_raw"), ("price", "currency"), ("price", "currency_source")),
    ("Price qualifier", ("price", "price_qualifier_raw"), None, None),
    ("Property type", ("attributes", "property_type_raw"), None, None),
    ("Bedrooms", ("attributes", "bedrooms_raw"), ("attributes", "bedrooms"), None),
    ("Bathrooms", ("attributes", "bathrooms_raw"), ("attributes", "bathrooms"), None),
    ("Floor area", ("attributes", "floor_area_raw"), ("attributes", "floor_area"),
     ("attributes", "floor_area_unit")),
    ("Lot size", ("attributes", "lot_size_raw"), ("attributes", "lot_size"),
     ("attributes", "lot_size_unit")),
    ("Year built", ("attributes", "year_built_raw"), None, None),
    ("Condition", ("attributes", "condition_raw"), None, None),
    ("Parish", ("location", "parish_raw"), None, None),
    ("Address", ("location", "address_raw"), ("location", "community"), None),
]

# raw present but parsed missing = parser gap worth looking at
PARSE_GAP_PAIRS = [
    ("price.amount_raw → amount", ("price", "amount_raw"), ("price", "amount")),
    ("price.currency_raw → currency", ("price", "currency_raw"), ("price", "currency")),
    ("bedrooms_raw → bedrooms", ("attributes", "bedrooms_raw"), ("attributes", "bedrooms")),
    ("bathrooms_raw → bathrooms", ("attributes", "bathrooms_raw"), ("attributes", "bathrooms")),
    ("floor_area_raw → floor_area", ("attributes", "floor_area_raw"), ("attributes", "floor_area")),
    ("lot_size_raw → lot_size", ("attributes", "lot_size_raw"), ("attributes", "lot_size")),
]


def _percentile(sorted_vals: list[float], q: float) -> float:
    return sorted_vals[min(len(sorted_vals) - 1, int(q * len(sorted_vals)))]


def _summarize(rows: list[dict]) -> dict:
    n = len(rows)

    def fill(sec, key):
        k = sum(1 for r in rows if _present(_g(r, sec, key)))
        return {"count": k, "rate": round(k / n, 4) if n else 0.0}

    fill_rates = [{"field": lab, **fill(s, k)} for lab, s, k in FILL_FIELDS]
    k = sum(1 for r in rows if r.get("images"))
    fill_rates.append({"field": "images (≥1)", "count": k, "rate": round(k / n, 4) if n else 0.0})

    gaps = []
    for lab, (rs, rk), (ps, pk) in PARSE_GAP_PAIRS:
        g = sum(1 for r in rows if _present(_g(r, rs, rk)) and not _present(_g(r, ps, pk)))
        gaps.append({"pair": lab, "raw_present_parsed_missing": g})

    matrix: dict[tuple, int] = defaultdict(int)
    for r in rows:
        matrix[(_label(_g(r, "attributes", "property_type_raw")),
                _label(_g(r, "listing", "listing_type_raw")))] += 1
    matrix_rows = [{"property_type": a, "listing_type": b, "count": c}
                   for (a, b), c in sorted(matrix.items(), key=lambda kv: -kv[1])]

    runs: dict[str, dict] = {}
    for r in rows:
        rid = _label(_g(r, "provenance", "scrape_run_id"))
        f = _g(r, "provenance", "fetched_at")
        e = runs.setdefault(rid, {"scrape_run_id": rid, "count": 0, "first_fetched_at": f, "last_fetched_at": f})
        e["count"] += 1
        if f:
            if not e["first_fetched_at"] or str(f) < str(e["first_fetched_at"]):
                e["first_fetched_at"] = f
            if not e["last_fetched_at"] or str(f) > str(e["last_fetched_at"]):
                e["last_fetched_at"] = f
    dates = Counter(_label(r.get("fetched_date") or _date_of(_g(r, "provenance", "fetched_at")))
                    for r in rows)

    by_cur: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        a = _g(r, "price", "amount")
        if isinstance(a, (int, float)):
            by_cur[_label(_g(r, "price", "currency"))].append(float(a))
    price_by_currency = []
    for cur, vals in sorted(by_cur.items()):
        vals.sort()
        price_by_currency.append({"currency": cur, "count": len(vals), "min": vals[0],
                                  "p50": _percentile(vals, 0.5), "max": vals[-1]})

    q = lambda sec, key: sum(1 for r in rows if _present(_g(r, sec, key)))  # noqa: E731
    return {
        "total": n,
        "coverage_note": COVERAGE_NOTE,
        "source": _cache["source"],
        "fill_rates": fill_rates,
        "parse_gaps": gaps,
        "amount_parse_status": _counts(rows, lambda r: _g(r, "price", "amount_parse_status")),
        "currency": _counts(rows, lambda r: _g(r, "price", "currency")),
        "currency_source": _counts(rows, lambda r: _g(r, "price", "currency_source")),
        "geo_source": _counts(rows, lambda r: _g(r, "location", "geo_source")),
        "geo_precision": _counts(rows, lambda r: _g(r, "location", "geo_precision")),
        "parish": _counts(rows, lambda r: _g(r, "location", "parish_raw")),
        "property_type": _counts(rows, lambda r: _g(r, "attributes", "property_type_raw")),
        "listing_type": _counts(rows, lambda r: _g(r, "listing", "listing_type_raw")),
        "property_by_listing_type": matrix_rows,
        "http_status": _counts(rows, lambda r: _g(r, "provenance", "http_status")),
        "parser_version": _counts(rows, lambda r: _g(r, "provenance", "parser_version")),
        "robots_allowed": _counts(rows, lambda r: _g(r, "provenance", "robots_allowed")
                                  if _g(r, "provenance", "robots_allowed") is not None else "(missing)"),
        "rows_per_run": sorted(runs.values(), key=lambda e: str(e["first_fetched_at"])),
        "rows_per_fetch_date": [{"fetched_date": d, "count": c} for d, c in sorted(dates.items())],
        "price_by_currency": price_by_currency,
        "quality_flags": {
            "with_parse_errors": sum(1 for r in rows if (r.get("quality") or {}).get("parse_errors")),
            "with_missing_required": sum(1 for r in rows if (r.get("quality") or {}).get("missing_required")),
            "marked_duplicate": q("quality", "duplicate_of"),
        },
    }


def _list_item(r: dict) -> dict:
    imgs = r.get("images") or []
    primary = next((i for i in imgs if i.get("is_primary")), imgs[0] if imgs else None)
    return {
        "record_id": r["record"]["record_id"],
        "site_listing_ref": _g(r, "provenance", "site_listing_ref"),
        "title": _g(r, "listing", "title"),
        "listing_type_raw": _g(r, "listing", "listing_type_raw"),
        "property_type_raw": _g(r, "attributes", "property_type_raw"),
        "amount": _g(r, "price", "amount"),
        "amount_raw": _g(r, "price", "amount_raw"),
        "currency": _g(r, "price", "currency"),
        "amount_parse_status": _g(r, "price", "amount_parse_status"),
        "community": _g(r, "location", "community"),
        "parish_raw": _g(r, "location", "parish_raw"),
        "latitude": _g(r, "location", "latitude"),
        "longitude": _g(r, "location", "longitude"),
        "geo_precision": _g(r, "location", "geo_precision"),
        "bedrooms": _g(r, "attributes", "bedrooms"),
        "bathrooms": _g(r, "attributes", "bathrooms"),
        "floor_area": _g(r, "attributes", "floor_area"),
        "floor_area_unit": _g(r, "attributes", "floor_area_unit"),
        "lot_size": _g(r, "attributes", "lot_size"),
        "image_count": len(imgs),
        "primary_image_url": primary.get("source_url") if primary else None,
        "scrape_run_id": _g(r, "provenance", "scrape_run_id"),
        "fetched_at": _g(r, "provenance", "fetched_at"),
        "has_parse_errors": bool((r.get("quality") or {}).get("parse_errors")),
    }


def _comparison(r: dict) -> list[dict]:
    out = []
    for label, (rs, rk), parsed, extra in COMPARE_FIELDS:
        raw = _g(r, rs, rk)
        val = _g(r, *parsed) if parsed else None
        ex = _g(r, *extra) if extra else None
        if parsed is None:
            status = "raw_only" if _present(raw) else "missing"
        elif _present(raw) and _present(val):
            status = "parsed"
        elif _present(raw):
            status = "unparsed"        # parser gap
        elif _present(val):
            status = "derived"         # parsed value with no raw source text
        else:
            status = "missing"
        out.append({"field": label, "raw": raw, "parsed": val, "extra": ex, "status": status})
    return out


SORTS = {
    "price_desc": (lambda i: i["amount"], True),
    "price_asc": (lambda i: i["amount"], False),
    "area_desc": (lambda i: i["floor_area"], True),
    "fetched_desc": (lambda i: i["fetched_at"], True),
}


# ---------- endpoints ----------

@router.get("/summary")
def listings_summary():
    """Data-quality overview: fill rates, parse status, category counts, per-run / per-date rows."""
    return _summarize(_rows())


@router.get("/filters")
def listings_filters():
    rows = _rows()

    def opts(getter):
        return sorted({str(v) for r in rows if _present(v := getter(r))})

    return {
        "coverage_note": COVERAGE_NOTE,
        "parish": opts(lambda r: _g(r, "location", "parish_raw")),
        "property_type": opts(lambda r: _g(r, "attributes", "property_type_raw")),
        "listing_type": opts(lambda r: _g(r, "listing", "listing_type_raw")),
        "currency": opts(lambda r: _g(r, "price", "currency")),
        "amount_parse_status": opts(lambda r: _g(r, "price", "amount_parse_status")),
        "geo_precision": opts(lambda r: _g(r, "location", "geo_precision")),
        "scrape_run_id": opts(lambda r: _g(r, "provenance", "scrape_run_id")),
        "missing_field": [f for f, _, _ in FILL_FIELDS],
        "sort": list(SORTS),
    }


def _filters(
    search: Optional[str] = None,
    parish: Optional[str] = None,
    property_type: Optional[str] = None,
    listing_type: Optional[str] = None,
    currency: Optional[str] = None,
    amount_parse_status: Optional[str] = None,
    geo_precision: Optional[str] = None,
    scrape_run_id: Optional[str] = None,
    missing_field: Optional[str] = Query(None, description="Only rows where this field (e.g. attributes.bedrooms) is empty"),
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    latest_only: bool = Query(False, description="Keep only the latest observation per site_listing_ref"),
) -> dict:
    """Grid filters as a dependency, shared by the grid, distributions and map endpoints."""
    if missing_field and not any(f == missing_field for f, _, _ in FILL_FIELDS):
        raise HTTPException(422, f"Unknown missing_field {missing_field!r}")
    return dict(search=search, parish=parish, property_type=property_type, listing_type=listing_type,
                currency=currency, amount_parse_status=amount_parse_status, geo_precision=geo_precision,
                scrape_run_id=scrape_run_id, missing_field=missing_field,
                min_price=min_price, max_price=max_price, latest_only=latest_only)


def _matching(f: dict) -> list[dict]:
    """Raw rows passing the filters (the `search` needle is checked on title/community/ref/id)."""
    missing = None
    if f["missing_field"]:
        missing = next((s, k) for lab, s, k in FILL_FIELDS if lab == f["missing_field"])

    def eq(r, sec, key, want):
        return want is None or str(_g(r, sec, key)) == want

    needle = f["search"].lower() if f["search"] else None
    out = []
    for r in _rows():
        if not (eq(r, "location", "parish_raw", f["parish"])
                and eq(r, "attributes", "property_type_raw", f["property_type"])
                and eq(r, "listing", "listing_type_raw", f["listing_type"])
                and eq(r, "price", "currency", f["currency"])
                and eq(r, "price", "amount_parse_status", f["amount_parse_status"])
                and eq(r, "location", "geo_precision", f["geo_precision"])
                and eq(r, "provenance", "scrape_run_id", f["scrape_run_id"])):
            continue
        if missing and _present(_g(r, *missing)):
            continue
        amt = _g(r, "price", "amount")
        if f["min_price"] is not None and (amt is None or amt < f["min_price"]):
            continue
        if f["max_price"] is not None and (amt is None or amt > f["max_price"]):
            continue
        if needle and needle not in " ".join(
                str(x or "") for x in (_g(r, "listing", "title"), _g(r, "location", "community"),
                                       _g(r, "provenance", "site_listing_ref"),
                                       r["record"]["record_id"])).lower():
            continue
        out.append(r)
    if f.get("latest_only"):
        out = _latest_per_ref(out)
    return out


def _latest_per_ref(rows: list[dict]) -> list[dict]:
    """One row per site_listing_ref (the most recently fetched); rows with no ref are kept as-is."""
    best: dict[str, dict] = {}
    for r in rows:
        ref = _g(r, "provenance", "site_listing_ref")
        if not ref:
            continue
        cur = best.get(ref)
        if cur is None or str(_g(r, "provenance", "fetched_at") or "") > str(_g(cur, "provenance", "fetched_at") or ""):
            best[ref] = r
    return [r for r in rows if not _g(r, "provenance", "site_listing_ref")
            or best[_g(r, "provenance", "site_listing_ref")] is r]


@router.get("")
def list_listings(
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=200),
    f: dict = Depends(_filters),
    sort: str = "price_desc",
):
    if sort not in SORTS:
        raise HTTPException(422, f"sort must be one of {list(SORTS)}")
    items = [_list_item(r) for r in _matching(f)]
    key, desc = SORTS[sort]
    have = [i for i in items if key(i) is not None]
    lack = [i for i in items if key(i) is None]
    have.sort(key=key, reverse=desc)
    items = have + lack  # rows lacking the sort key always go last
    total = len(items)
    start = (page - 1) * page_size
    return {"total": total, "page": page, "page_size": page_size,
            "coverage_note": COVERAGE_NOTE, "items": items[start:start + page_size]}


# ---------- distributions ----------

OUTLIER_MIN_N = 8       # don't fence facets smaller than this
OUTLIER_K = 1.5         # Tukey fence multiplier on log10 values
EXTREME_K = 3.0         # beyond this the outlier is "extreme"
N_BINS = 20
# Price per area is only shown for these units; anything else is skipped rather than guessed at.
COMPARABLE_AREA_UNITS = {"sqft", "sqm"}


def _quantile(sorted_vals: list[float], q: float) -> float:
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def _log_bins(vals: list[float], n_bins: int = N_BINS) -> list[dict]:
    """Equal-width bins on log10(value); values must be > 0."""
    logs = [math.log10(v) for v in vals]
    lo, hi = min(logs), max(logs)
    if hi == lo:
        return [{"lo": vals[0], "hi": vals[0], "count": len(vals)}]
    w = (hi - lo) / n_bins
    counts = [0] * n_bins
    for x in logs:
        counts[min(n_bins - 1, int((x - lo) / w))] += 1
    return [{"lo": 10 ** (lo + i * w), "hi": 10 ** (lo + (i + 1) * w), "count": c}
            for i, c in enumerate(counts)]


def _fences(vals: list[float]) -> Optional[dict]:
    """Tukey fences on log10 values, returned on the original scale."""
    if len(vals) < OUTLIER_MIN_N:
        return None
    logs = sorted(math.log10(v) for v in vals)
    q1, q3 = _quantile(logs, 0.25), _quantile(logs, 0.75)
    iqr = q3 - q1
    return {"q1": 10 ** q1, "q3": 10 ** q3,
            "low": 10 ** (q1 - OUTLIER_K * iqr), "high": 10 ** (q3 + OUTLIER_K * iqr),
            "low_extreme": 10 ** (q1 - EXTREME_K * iqr), "high_extreme": 10 ** (q3 + EXTREME_K * iqr)}


def _num(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _facet(metric: str, label: str, facet: str, unit: Optional[str],
           pairs: list[tuple[dict, float]], outliers: list[dict]) -> dict:
    """pairs = (row, value). Appends flagged rows to `outliers`. Non-positive values can't go on a
    log axis, so they are excluded from the histogram and only counted."""
    pos = [(r, v) for r, v in pairs if v > 0]
    fences = _fences([v for _, v in pos])
    if fences:
        for r, v in pos:
            side = "high" if v > fences["high"] else "low" if v < fences["low"] else None
            if not side:
                continue
            extreme = v > fences["high_extreme"] if side == "high" else v < fences["low_extreme"]
            it = _list_item(r)
            outliers.append({"metric": metric, "facet": facet, "unit": unit, "value": v, "side": side,
                             "severity": "extreme" if extreme else "mild",
                             "fence": fences["high"] if side == "high" else fences["low"],
                             "record_id": it["record_id"], "title": it["title"],
                             "property_type_raw": it["property_type_raw"],
                             "amount": it["amount"], "currency": it["currency"],
                             "amount_raw": it["amount_raw"]})
    vals = sorted(v for _, v in pos)
    return {"metric": metric, "label": label, "facet": facet, "unit": unit, "n": len(pos),
            "non_positive": len(pairs) - len(pos),
            "min": vals[0] if vals else None, "max": vals[-1] if vals else None,
            "median": _quantile(vals, 0.5) if vals else None,
            "fences": fences, "bins": _log_bins(vals) if vals else []}


@router.get("/distributions")
def listings_distributions(f: dict = Depends(_filters)):
    """Server-side histograms (log10 bins) split by currency / unit — never mixing units on one
    axis — plus IQR-on-log10 outlier flags and the property type x listing type mix."""
    rows = _matching(f)
    excluded = len(_matching({**f, "latest_only": False})) - len(rows) if f.get("latest_only") else 0
    outliers: list[dict] = []
    facets: list[dict] = []

    def grouped(metric, label, section, key, keyf):
        groups: dict[str, list] = defaultdict(list)
        for r in rows:
            v = _num(_g(r, section, key))
            if v is not None:
                groups[keyf(r)].append((r, v))
        for g, pairs in sorted(groups.items()):
            facets.append(_facet(metric, label, g, g if metric != "price" else None, pairs, outliers))

    grouped("price", "Asking price", "price", "amount", lambda r: _label(_g(r, "price", "currency")))
    grouped("floor_area", "Floor area", "attributes", "floor_area",
            lambda r: _label(_g(r, "attributes", "floor_area_unit")))
    grouped("lot_size", "Lot size", "attributes", "lot_size",
            lambda r: _label(_g(r, "attributes", "lot_size_unit")))

    ppu_groups: dict[str, list] = defaultdict(list)
    skipped = 0
    for r in rows:
        area, price = _num(_g(r, "attributes", "floor_area")), _num(_g(r, "price", "amount"))
        if not (area and price and area > 0 and price > 0):
            continue
        unit = _g(r, "attributes", "floor_area_unit")
        if unit not in COMPARABLE_AREA_UNITS:
            skipped += 1
            continue
        ppu_groups[f"{_label(_g(r, 'price', 'currency'))} / {unit}"].append((r, price / area))
    for g, pairs in sorted(ppu_groups.items()):
        facets.append(_facet("price_per_area", "Price per floor-area unit", g, g.split(" / ")[1],
                             pairs, outliers))

    beds = Counter(b for r in rows if (b := _num(_g(r, "attributes", "bedrooms"))) is not None)
    bedrooms = [{"bedrooms": int(k) if float(k).is_integer() else k, "count": n}
                for k, n in sorted(beds.items())]

    matrix: dict[tuple, int] = defaultdict(int)
    for r in rows:
        matrix[(_label(_g(r, "attributes", "property_type_raw")),
                _label(_g(r, "listing", "listing_type_raw")))] += 1
    ptypes = Counter(_label(_g(r, "attributes", "property_type_raw")) for r in rows)

    outliers.sort(key=lambda o: (o["severity"] != "extreme", -abs(math.log10(o["value"] / o["fence"]))))
    return {
        "coverage_note": COVERAGE_NOTE, "total": len(rows), "duplicates_excluded": excluded,
        "facets": facets, "bedrooms": bedrooms,
        "bedrooms_missing": len(rows) - sum(beds.values()),
        "price_per_area_skipped_incomparable_unit": skipped,
        "outliers": outliers,
        "outlier_rule": (f"log10(value) outside Q1-{OUTLIER_K}·IQR … Q3+{OUTLIER_K}·IQR within each "
                         f"currency/unit facet (extreme beyond {EXTREME_K}·IQR); facets with "
                         f"n<{OUTLIER_MIN_N} are not fenced"),
        "property_by_listing_type": [{"property_type": a, "listing_type": b, "count": c}
                                     for (a, b), c in sorted(matrix.items(), key=lambda kv: -kv[1])],
        "property_type_totals": [{"property_type": k, "count": n} for k, n in ptypes.most_common()],
    }


# ---------- map ----------

JAMAICA_BBOX = {"lat_min": 17.6, "lat_max": 18.6, "lon_min": -78.5, "lon_max": -76.1}
CLUSTER_MIN = 3


def _in_jamaica(lat: float, lon: float) -> bool:
    b = JAMAICA_BBOX
    return b["lat_min"] <= lat <= b["lat_max"] and b["lon_min"] <= lon <= b["lon_max"]


def _geo_problem(lat: float, lon: float) -> Optional[str]:
    if _in_jamaica(lat, lon):
        return None
    if (lat, lon) == (0, 0):
        return "null_island"
    if _in_jamaica(lon, lat):
        return "lat_lon_swapped"
    if lon > 0 and _in_jamaica(lat, -lon):
        return "longitude_sign_lost"
    return "outside_jamaica"


@router.get("/geo")
def listings_geo(f: dict = Depends(_filters), min_cluster: int = Query(CLUSTER_MIN, ge=2)):
    """Listings with coordinates, bounding-box problems, and clusters of listings sharing identical
    coordinates (typically a parish/community centroid fallback)."""
    rows = _matching(f)
    excluded = len(_matching({**f, "latest_only": False})) - len(rows) if f.get("latest_only") else 0
    points: list[dict] = []
    by_coord: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        lat, lon = _num(_g(r, "location", "latitude")), _num(_g(r, "location", "longitude"))
        if lat is None or lon is None:
            continue
        it = _list_item(r)
        p = {k: it[k] for k in ("record_id", "site_listing_ref", "title", "property_type_raw", "listing_type_raw", "amount",
                                "currency", "community", "parish_raw", "geo_precision")}
        p.update(latitude=lat, longitude=lon, geo_source=_g(r, "location", "geo_source"),
                 problem=_geo_problem(lat, lon))
        points.append(p)
        by_coord[(round(lat, 5), round(lon, 5))].append(p)
    def distinct(members):  # the same listing observed in several runs is not a shared centroid
        return len({m["site_listing_ref"] or m["record_id"] for m in members})

    for p in points:
        members = by_coord[(round(p["latitude"], 5), round(p["longitude"], 5))]
        p["shared_count"] = distinct(members)

    clusters = []
    for (lat, lon), members in by_coord.items():
        if distinct(members) < min_cluster:
            continue
        clusters.append({
            "latitude": lat, "longitude": lon, "count": distinct(members), "observations": len(members),
            "communities": sorted({_label(m["community"]) for m in members}),
            "parishes": sorted({_label(m["parish_raw"]) for m in members}),
            "geo_precision": dict(Counter(_label(m["geo_precision"]) for m in members)),
            "geo_source": dict(Counter(_label(m["geo_source"]) for m in members)),
            "property_types": dict(Counter(_label(m["property_type_raw"]) for m in members)),
            "record_ids": [m["record_id"] for m in members][:100],
            "problem": _geo_problem(lat, lon),
        })
    clusters.sort(key=lambda c: -c["count"])
    return {
        "coverage_note": COVERAGE_NOTE, "bbox": JAMAICA_BBOX, "total": len(rows), "duplicates_excluded": excluded,
        "with_coordinates": len(points), "without_coordinates": len(rows) - len(points),
        "geo_precision": dict(Counter(_label(p["geo_precision"]) for p in points)),
        "geo_source": dict(Counter(_label(p["geo_source"]) for p in points)),
        "points": points, "outside_bbox": [p for p in points if p["problem"]],
        "clusters": clusters, "points_in_clusters": sum(c["observations"] for c in clusters),
        "min_cluster": min_cluster,
    }


# ---------- run comparison ----------

# (label, section, key) compared between the two observations of the same listing.
DIFF_FIELDS = [
    ("Price", "price", "amount"),
    ("Currency", "price", "currency"),
    ("Status", "listing", "listing_status_raw"),
    ("Listing type", "listing", "listing_type_raw"),
    ("Property type", "attributes", "property_type_raw"),
    ("Title", "listing", "title"),
    ("Bedrooms", "attributes", "bedrooms"),
    ("Bathrooms", "attributes", "bathrooms"),
    ("Floor area", "attributes", "floor_area"),
    ("Floor area unit", "attributes", "floor_area_unit"),
    ("Lot size", "attributes", "lot_size"),
    ("Lot size unit", "attributes", "lot_size_unit"),
    ("Year built", "attributes", "year_built_raw"),
    ("Condition", "attributes", "condition_raw"),
    ("Parish", "location", "parish_raw"),
    ("Community", "location", "community"),
    ("Latitude", "location", "latitude"),
    ("Longitude", "location", "longitude"),
    ("Listed at", "listing", "listed_at"),
    ("Agent", "agent", "agent_name_raw"),
    ("Agency", "agent", "agency_name_raw"),
]
DIFF_LIST_CAP = 1000


def _listing_key(r: dict) -> Optional[str]:
    """Identity of a listing across runs. record_id is per observation, so it can't be used."""
    return _g(r, "provenance", "site_listing_ref") or _g(r, "provenance", "canonical_url")


def _run_index(rows: list[dict], run_id: str) -> tuple[dict[str, dict], int]:
    """{listing key: latest observation} for one run, and how many rows had no usable key."""
    idx: dict[str, dict] = {}
    unkeyed = 0
    for r in rows:
        if _g(r, "provenance", "scrape_run_id") != run_id:
            continue
        k = _listing_key(r)
        if not k:
            unkeyed += 1
            continue
        cur = idx.get(k)
        if cur is None or str(_g(r, "provenance", "fetched_at") or "") >= str(_g(cur, "provenance", "fetched_at") or ""):
            idx[k] = r
    return idx, unkeyed


def _same(a: Any, b: Any) -> bool:
    if not _present(a) and not _present(b):
        return True
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)
    return a == b


def diff_runs(rows: list[dict], run_a: str, run_b: str) -> dict:
    """Pure diff of two scrape runs, keyed by site_listing_ref (canonical_url fallback)."""
    ia, unkeyed_a = _run_index(rows, run_a)
    ib, unkeyed_b = _run_index(rows, run_b)
    only_a = [_list_item(ia[k]) for k in sorted(ia.keys() - ib.keys())]
    only_b = [_list_item(ib[k]) for k in sorted(ib.keys() - ia.keys())]
    both_keys = sorted(ia.keys() & ib.keys())

    changed, unchanged = [], 0
    field_counts: Counter = Counter()
    parser_changed = 0
    for k in both_keys:
        a, b = ia[k], ib[k]
        changes = [{"field": lab, "before": _g(a, s, key), "after": _g(b, s, key)}
                   for lab, s, key in DIFF_FIELDS if not _same(_g(a, s, key), _g(b, s, key))]
        pv = (_g(a, "provenance", "parser_version"), _g(b, "provenance", "parser_version"))
        parser_changed += pv[0] != pv[1]
        if not changes:
            unchanged += 1
            continue
        for c in changes:
            field_counts[c["field"]] += 1
        ca, cb = _list_item(a), _list_item(b)
        changed.append({"key": k, "title": cb["title"] or ca["title"],
                        "record_id_a": ca["record_id"], "record_id_b": cb["record_id"],
                        "fetched_at_a": ca["fetched_at"], "fetched_at_b": cb["fetched_at"],
                        "parser_version_a": pv[0], "parser_version_b": pv[1],
                        "parser_version_changed": pv[0] != pv[1], "changes": changes})
    return {
        "run_a": run_a, "run_b": run_b,
        "counts": {"a": len(ia), "b": len(ib), "only_a": len(only_a), "only_b": len(only_b),
                   "both": len(both_keys), "both_changed": len(changed), "both_unchanged": unchanged,
                   "unkeyed_a": unkeyed_a, "unkeyed_b": unkeyed_b,
                   "parser_version_differs": parser_changed},
        "field_change_counts": [{"field": f, "count": n} for f, n in field_counts.most_common()],
        "only_a": only_a[:DIFF_LIST_CAP], "only_b": only_b[:DIFF_LIST_CAP],
        "changed": changed[:DIFF_LIST_CAP],
        "truncated": any(n > DIFF_LIST_CAP for n in (len(only_a), len(only_b), len(changed))),
        "compared_fields": [lab for lab, _, _ in DIFF_FIELDS],
    }


def run_overview(rows: list[dict]) -> dict:
    """Which scrape runs exist and how much the same listing recurs across them."""
    per_run: dict[str, dict] = {}
    runs_of: dict[str, set] = defaultdict(set)
    for r in rows:
        rid = _g(r, "provenance", "scrape_run_id")
        if not rid:
            continue
        f = _g(r, "provenance", "fetched_at")
        e = per_run.setdefault(rid, {"scrape_run_id": rid, "observations": 0, "first_fetched_at": f,
                                     "last_fetched_at": f, "parser_versions": set()})
        e["observations"] += 1
        e["parser_versions"].add(_label(_g(r, "provenance", "parser_version")))
        if f:
            if not e["first_fetched_at"] or str(f) < str(e["first_fetched_at"]):
                e["first_fetched_at"] = f
            if not e["last_fetched_at"] or str(f) > str(e["last_fetched_at"]):
                e["last_fetched_at"] = f
        k = _listing_key(r)
        if k:
            runs_of[k].add(rid)
    for e in per_run.values():
        e["listings"] = sum(1 for s in runs_of.values() if e["scrape_run_id"] in s)
        e["parser_versions"] = sorted(e["parser_versions"])
    runs = sorted(per_run.values(), key=lambda e: (str(e["first_fetched_at"]), e["scrape_run_id"]))
    ids = [e["scrape_run_id"] for e in runs]
    pairs = []
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            n = sum(1 for s in runs_of.values() if a in s and b in s)
            pairs.append({"run_a": a, "run_b": b, "shared": n})
    hist = Counter(len(s) for s in runs_of.values())
    multi = sum(n for k, n in hist.items() if k > 1)
    best = max(pairs, key=lambda p: p["shared"], default=None)
    return {
        "coverage_note": COVERAGE_NOTE, "source": _cache["source"], "runs": runs,
        "distinct_listings": len(runs_of), "observations": len(rows),
        "listings_in_multiple_runs": multi,
        "listings_by_run_count": [{"runs": k, "listings": n} for k, n in sorted(hist.items())],
        "pair_overlap": pairs,
        "best_pair": best if best and best["shared"] else None,
        "comparable": multi > 0,
    }


def latest_run_new_listings(rows: list[dict]) -> Optional[dict]:
    """The latest scrape run and the listings that appear in it and in no other run (None when there are no runs).

    "Latest" is the run with the newest first fetch; a listing is identified as in ``run_overview`` (site_listing_ref,
    canonical_url as the fallback), and when a run holds a listing twice its most recent observation is used. Rows
    without a usable key are only counted, since "new" cannot be decided for them.
    """
    first_fetch: dict[str, Any] = {}
    for r in rows:
        rid = _g(r, "provenance", "scrape_run_id")
        if not rid:
            continue
        f = _g(r, "provenance", "fetched_at")
        if rid not in first_fetch or (f and (not first_fetch[rid] or str(f) < str(first_fetch[rid]))):
            first_fetch[rid] = f
    if not first_fetch:
        return None
    latest = max(first_fetch, key=lambda rid: (str(first_fetch[rid] or ""), rid))
    in_latest: dict[str, dict] = {}
    elsewhere: set[str] = set()
    unkeyed = 0
    for r in rows:
        rid = _g(r, "provenance", "scrape_run_id")
        k = _listing_key(r)
        if rid == latest:
            if not k:
                unkeyed += 1
                continue
            cur = in_latest.get(k)
            if cur is None or str(_g(r, "provenance", "fetched_at") or "") >= str(_g(cur, "provenance", "fetched_at") or ""):
                in_latest[k] = r
        elif rid and k:
            elsewhere.add(k)
    new = [in_latest[k] for k in sorted(in_latest.keys() - elsewhere)]
    return {"run_id": latest, "first_fetched_at": first_fetch[latest], "earlier_runs": len(first_fetch) - 1,
            "listings_in_run": len(in_latest), "unkeyed_rows": unkeyed, "new": new}


@router.get("/runs")
def listings_runs():
    """Scrape runs, per-run listing counts and cross-run overlap (is a run diff even meaningful?)."""
    return run_overview(_rows())


@router.get("/runs/diff")
def listings_run_diff(run_a: str, run_b: str):
    """Listings only in A, only in B, in both — and field-level before/after for those in both."""
    rows = _rows()
    known = {_g(r, "provenance", "scrape_run_id") for r in rows}
    for rid in (run_a, run_b):
        if rid not in known:
            raise HTTPException(404, f"Unknown scrape run {rid!r}")
    if run_a == run_b:
        raise HTTPException(422, "Pick two different runs")
    return {"coverage_note": COVERAGE_NOTE, **diff_runs(rows, run_a, run_b)}


@router.get("/{record_id}")
def get_listing(record_id: str):
    for r in _rows():
        if r["record"]["record_id"] == record_id:
            imgs = sorted(r.get("images") or [], key=lambda i: i.get("order_index") or 0)
            key = _listing_key(r)
            others = [_list_item(o) for o in _rows()
                      if key and _listing_key(o) == key and o is not r]
            return {
                "coverage_note": COVERAGE_NOTE,
                "other_observations": [{k: o[k] for k in ("record_id", "scrape_run_id", "fetched_at", "amount", "currency")}
                                       for o in sorted(others, key=lambda o: str(o["fetched_at"]))],
                "record": r,
                "summary": _list_item(r),
                "comparison": _comparison(r),
                "provenance": r.get("provenance") or {},
                "images": imgs,
                "quality": r.get("quality") or {},
            }
    raise HTTPException(404, "Listing not found")
