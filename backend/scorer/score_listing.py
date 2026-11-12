"""Score one listing with the registered listing-price model.

Run under the ats_avm monorepo environment, not the explorer's venv (``avm_model`` pins scikit-learn):

    echo '<request json>' | uv run --package avm_model --extra prototype python tools/trident-val/backend/scorer/score_listing.py

Reads one request from stdin and prints one JSON line. Exit 0 even when a model slot failed (the error is in the
response); exit 2 with ``{"fatal": ...}`` when the input row cannot be built. Read only: BigQuery SELECTs, GCS reads
and MLflow reads; nothing is written.

``run`` is pure given its ``Deps``, so tests use fakes. The real dependencies are built, and ``avm_model`` imported,
only in ``main``.
"""

import json
import os
import sys
import threading
from collections.abc import Callable, Collection, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from scorer.input_row import blocking_flags, row_from_joined, row_from_raw  # noqa: E402

KEY_FIELDS = ("source_site_id", "site_listing_ref")
# The geo and spatial tables repeat the key and the coordinates; training keeps listing_clean's copy of the
# coordinates (avm_model.prototype.data.QUERY), so the fallback must not let these tables override the raw ones.
GEO_SPATIAL_SQL = (
    "SELECT * EXCEPT (source_site_id, site_listing_ref, latitude, longitude) FROM `{project}.{dataset}.{table}` "
    "WHERE source_site_id = @s AND site_listing_ref = @r"
)
FX_MAX_GAP_DAYS = 5  # avm_features.listing_config.fx_max_gap_days
FX_RATES_TABLE = "fx_rates_boj"
# The bulk lookups match the (site, listing ref) pairs through one concatenated string parameter.
KEY_IN_SQL = "CONCAT(source_site_id, '|', site_listing_ref) IN UNNEST(@keys)"
GEO_SPATIAL_MANY_SQL = (
    "SELECT * EXCEPT (latitude, longitude) FROM `{project}.{dataset}.{table}` WHERE {keys}"
)
RESULT_FIELDS = (
    "pred_price_jmd",
    "lower_80_jmd",
    "upper_80_jmd",
    "lower_90_jmd",
    "upper_90_jmd",
    "lower_95_jmd",
    "upper_95_jmd",
)


@dataclass
class Deps:
    columns: Callable[[], list[str]]
    fetch_joined: Callable[[str, str], dict | None]
    fetch_geo_spatial: Callable[[str, str], tuple[dict | None, dict | None]]
    fetch_page_fields: Callable[[str], dict]
    load_model: Callable[[str], Any]
    # Names of the string-valued model inputs; every other input is cast to float64 (see _frames).
    categorical: Callable[[], list[str]] = field(default_factory=lambda: (lambda: []))
    # Optional bulk lookups for the batch view, keyed by (source_site_id, site_listing_ref). Each returns a dict;
    # a key with no row is absent. fetch_joined_many maps a key to its row, or to an Exception (for example when the
    # key has several rows); fetch_geo_spatial_many maps a key to (geo row or None, spatial row or None). Without
    # them the batch code falls back to the per-listing functions above.
    fetch_joined_many: Callable[[list[tuple[str, str]]], dict] | None = None
    fetch_geo_spatial_many: Callable[[list[tuple[str, str]]], dict] | None = None
    # Bank of Jamaica USD rate for each fetch timestamp, by the rule listing_clean uses (the last published rate on or
    # before the Jamaica date, at most FX_MAX_GAP_DAYS back): [{"rate": mid, "date": "YYYY-MM-DD"} or None, ...].
    # Used only for a USD listing that is not in listing_clean, which has no rate of its own.
    fx_rates: Callable[[Sequence[Any]], list[dict | None]] | None = None
    # Drivers and comps for one listing (``explain_listing.make_explain``): (model ref, input row, key) -> dict.
    explain: Callable[[str, dict, dict], dict] | None = None


def _number(value: Any) -> float | None:
    if value is None or value != value:
        return None
    return float(value)


def _frames(rows: Sequence[dict[str, Any]], columns: Sequence[str], categorical: Collection[str]) -> Any:
    """Model input frame for one or many rows, built as batch scoring builds it (``score_frame``).

    Columns in ``columns`` order; non-categorical columns go through ``to_numeric(errors="coerce")`` to float64
    (the model signature wants doubles, so None, Decimal and bool values become NaN or floats); categorical
    columns stay as they are.
    """
    import pandas as pd

    frame = pd.DataFrame(list(rows)).reindex(columns=list(columns))
    for col in columns:
        if col not in categorical:
            frame[col] = pd.to_numeric(frame[col], errors="coerce").astype("float64")
    return frame.reset_index(drop=True)


def _frame(inputs: dict[str, Any], columns: Sequence[str], categorical: Collection[str]) -> Any:
    """One-row model input frame (see ``_frames``)."""
    return _frames([inputs], columns, categorical)


def _row_result(out: Any) -> dict[str, Any]:
    """One output row of ``model.predict`` as the JSON-able slot result."""
    result: dict[str, Any] = {"label": str(out["model_version"])}
    for field in RESULT_FIELDS:
        result[field] = _number(out[field])
    result["floor_area_flagged"] = bool(out["floor_area_flagged"])
    return result


def _slot_result(model: Any, frame: Any) -> dict[str, Any]:
    return _row_result(model.predict(frame).iloc[0])


def group_by_key(rows: Sequence[dict], drop_key: bool = False) -> dict:
    """{(source_site_id, site_listing_ref): row} for a bulk lookup. A key with several rows maps to the same
    ValueError the one-listing lookup raises, so that listing alone is reported as failed."""
    grouped: dict[tuple[str, str], list[dict]] = {}
    for found in rows:
        grouped.setdefault((found["source_site_id"], found["site_listing_ref"]), []).append(found)
    out: dict = {}
    for key, found in grouped.items():
        if len(found) > 1:
            out[key] = ValueError(f"{len(found)} rows for {key[0]}/{key[1]}")
        elif drop_key:
            out[key] = {k: v for k, v in found[0].items() if k not in KEY_FIELDS}
        else:
            out[key] = found[0]
    return out


def _clean_block(joined: dict) -> dict[str, Any]:
    return {
        "record_id": joined.get("record_id"),
        "fetched_at": joined.get("fetched_at"),
        "trainable": joined.get("trainable"),
        "fx_rate_used": joined.get("fx_rate_used"),
        "price_currency_original": joined.get("price_currency_original"),
        "price_jmd": joined.get("price_jmd"),
        "blocking_flags": blocking_flags(joined),
    }


def assemble_row(raw: dict, joined: dict | None, geo: dict | None, spatial: dict | None, page: dict,
                 columns: Sequence[str]) -> dict:
    """The model input row and its provenance from whichever sources were found (shared by one-listing and batch)."""
    if joined is not None:
        return {"input_source": "listing_clean", "inputs": row_from_joined(joined, page, columns),
                "clean": _clean_block(joined)}
    return {"input_source": "raw_fallback", "inputs": row_from_raw(raw or {}, geo, spatial, page, columns),
            "clean": None}


def build_row(request: dict, deps: Deps) -> dict:
    """Fetch what one listing needs and assemble its input row: ``input_source``, ``clean``, ``inputs``,
    ``page_error``. The page and the listing_clean lookup run side by side, the two fallback tables inside
    ``deps.fetch_geo_spatial`` likewise."""
    site, ref = (request["key"][k] for k in KEY_FIELDS)
    columns = deps.columns()

    def page_job() -> tuple[dict, str | None]:
        if not request.get("snapshot_uri"):
            return {}, None
        try:
            return deps.fetch_page_fields(request["snapshot_uri"]), None
        except Exception as exc:  # noqa: BLE001 - reported, scoring continues without page fields
            return {}, str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        page_future = pool.submit(page_job)
        joined = deps.fetch_joined(site, ref)
        page, page_error = page_future.result()
    geo = spatial = None
    if joined is None:
        geo, spatial = deps.fetch_geo_spatial(site, ref)
    raw = request.get("raw") or {}
    built = assemble_row(raw, joined, geo, spatial, page, columns)
    built["page_error"] = page_error
    built["fx"] = None
    if built["input_source"] == "raw_fallback" and raw.get("currency") == "USD" and deps.fx_rates is not None:
        try:
            built["fx"] = deps.fx_rates([raw.get("fetched_at")])[0]
        except Exception:  # noqa: BLE001 - without a rate the listing simply has no JMD asking price
            built["fx"] = None
    return built


def run(request: dict, deps: Deps, row: dict | None = None) -> dict:
    """Score one listing. ``row`` is an already built ``build_row`` result (the server reuses recent ones)."""
    built = row if row is not None else build_row(request, deps)
    frame = _frame(built["inputs"], deps.columns(), set(deps.categorical()))
    slots: dict[str, Any] = {}
    for name, model_ref in (request.get("slots") or {}).items():
        if model_ref is None:
            continue
        try:
            slots[name] = _slot_result(deps.load_model(model_ref), frame)
        except Exception as exc:  # noqa: BLE001 - one failed slot must not stop the other
            slots[name] = {"error": str(exc)}

    return {
        "input_source": built["input_source"],
        "clean": built["clean"],
        "inputs": built["inputs"],
        "slots": slots,
        "page_error": built["page_error"],
        "fx": built.get("fx"),
    }


def _json_default(value: Any) -> Any:
    from decimal import Decimal

    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "item"):  # numpy scalar
        return value.item()
    return str(value)  # datetimes, Timestamps


def _real_deps() -> Deps:
    from avm_model import bundle
    from avm_model.loader import load_model
    from avm_model.prototype import data, field_check, page_fields
    from google.cloud import bigquery

    client = bigquery.Client(project=data.PROJECT)
    pages_dir = Path(
        os.environ.get("MODEL_CHECK_PAGES_DIR") or BACKEND_DIR.parent / ".cache" / "pages"
    )

    def select_one(sql: str, site: str, ref: str) -> dict | None:
        job = client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("s", "STRING", site),
                    bigquery.ScalarQueryParameter("r", "STRING", ref),
                ]
            ),
        )
        rows = [dict(row) for row in job.result()]
        if len(rows) > 1:
            raise ValueError(f"{len(rows)} rows for {site}/{ref}")
        return rows[0] if rows else None

    def fetch_joined(site: str, ref: str) -> dict | None:
        return select_one(
            f"SELECT * FROM ({data.QUERY}) WHERE source_site_id = @s AND site_listing_ref = @r", site, ref
        )

    def fetch_geo_spatial(site: str, ref: str) -> tuple[dict | None, dict | None]:
        def one(table: str) -> dict | None:
            sql = GEO_SPATIAL_SQL.format(project=data.PROJECT, dataset=data.DATASET, table=table)
            return select_one(sql, site, ref)

        with ThreadPoolExecutor(max_workers=2) as pool:
            geo, spatial = pool.map(one, ("listing_geo_features", "listing_spatial_features"))
        return geo, spatial

    def select_many(sql: str, keys: list[tuple[str, str]]) -> list[dict]:
        job = client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ArrayQueryParameter("keys", "STRING", [f"{s}|{r}" for s, r in keys])
                ]
            ),
        )
        return [dict(row) for row in job.result()]

    def fetch_joined_many(keys: list[tuple[str, str]]) -> dict:
        rows = select_many(f"SELECT * FROM ({data.QUERY}) WHERE {KEY_IN_SQL}", keys)
        return group_by_key(rows)

    def fetch_geo_spatial_many(keys: list[tuple[str, str]]) -> dict:
        def one(table: str) -> dict:
            sql = GEO_SPATIAL_MANY_SQL.format(project=data.PROJECT, dataset=data.DATASET, table=table, keys=KEY_IN_SQL)
            return group_by_key(select_many(sql, keys), drop_key=True)

        with ThreadPoolExecutor(max_workers=2) as pool:
            geo, spatial = pool.map(one, ("listing_geo_features", "listing_spatial_features"))
        out: dict = {}
        for key in geo.keys() | spatial.keys():
            found = (geo.get(key), spatial.get(key))
            bad = next((f for f in found if isinstance(f, Exception)), None)
            out[key] = bad if bad is not None else found
        return out

    storage_state: dict[str, Any] = {"client": None, "failed": False}
    storage_lock = threading.Lock()

    def storage_client() -> Any:
        """One GCS client for every page (creating one costs seconds); None if the library is missing."""
        with storage_lock:
            if storage_state["client"] is None and not storage_state["failed"]:
                try:
                    from google.cloud import storage

                    storage_state["client"] = storage.Client(project=data.PROJECT)
                except Exception:  # noqa: BLE001 - fall back to gcloud for every page
                    storage_state["failed"] = True
            return storage_state["client"]

    rates_state: dict[str, Any] = {"frame": None}
    rates_lock = threading.Lock()

    def load_rates() -> Any:
        with rates_lock:
            if rates_state["frame"] is None:
                sql = (f"SELECT rate_date, buying, selling, mid FROM "
                       f"`{data.PROJECT}.{data.DATASET}.{FX_RATES_TABLE}` ORDER BY rate_date")
                rates_state["frame"] = client.query(sql).to_dataframe()
            return rates_state["frame"]

    def fx_rates(timestamps: Sequence[Any]) -> list[dict | None]:
        import pandas as pd
        from avm_features import listing_fx

        rates = load_rates()
        if rates.empty:
            return [None] * len(timestamps)
        days = listing_fx.jamaica_date(pd.Series(list(timestamps), dtype=object))
        got = listing_fx.attach_fx_rates(days, rates, max_gap_days=FX_MAX_GAP_DAYS)
        return [
            {"rate": float(rate), "date": str(day)} if pd.notna(rate) else None
            for rate, day in zip(got["fx_rate_mid"], got["fx_rate_date"])
        ]

    def download_page(uri: str) -> None:
        """Put the stored page in the local cache with the GCS client, which is far quicker than starting
        ``gcloud storage cat`` for every page. Any failure is left to ``field_check.fetch_page`` (gcloud)."""
        path = pages_dir / uri.rsplit("/", 2)[-2] / uri.rsplit("/", 1)[-1]
        if path.exists():
            return
        try:
            client = storage_client()
            if client is None:
                return
            bucket, _, name = uri.removeprefix("gs://").partition("/")
            blob = client.bucket(bucket).blob(name).download_as_bytes()
            path.parent.mkdir(parents=True, exist_ok=True)
            part = path.with_name(path.name + f".{threading.get_ident()}.part")
            part.write_bytes(blob)
            part.replace(path)
        except Exception:  # noqa: BLE001 - the gcloud fallback below reports the real problem
            pass

    def fetch_page_fields(uri: str) -> dict:
        download_page(uri)
        return page_fields.extract(field_check.fetch_page(uri, pages_dir))

    models: dict[str, Any] = {}

    def cached_model(ref: str) -> Any:
        if ref not in models:
            models[ref] = load_model(ref)
        return models[ref]

    from scorer.explain_listing import make_explain

    return Deps(
        columns=bundle.input_columns,
        fetch_joined=fetch_joined,
        fetch_geo_spatial=fetch_geo_spatial,
        fetch_page_fields=fetch_page_fields,
        load_model=cached_model,
        categorical=bundle.categorical_inputs,
        fetch_joined_many=fetch_joined_many,
        fetch_geo_spatial_many=fetch_geo_spatial_many,
        fx_rates=fx_rates,
        explain=make_explain(cached_model, bundle.input_columns, bundle.categorical_inputs, _frames),
    )


def main() -> int:
    stdout = sys.stdout
    sys.stdout = sys.stderr  # library chatter must not corrupt the one JSON line
    try:
        request = json.load(sys.stdin)
        response = run(request, _real_deps())
        code = 0
    except Exception as exc:  # noqa: BLE001 - the input row could not be built
        response = {"fatal": f"{type(exc).__name__}: {exc}"}
        code = 2
    stdout.write(json.dumps(response, default=_json_default) + "\n")
    stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())
