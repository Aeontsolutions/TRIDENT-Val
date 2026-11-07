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
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from scorer.input_row import blocking_flags, row_from_joined, row_from_raw  # noqa: E402

KEY_FIELDS = ("source_site_id", "site_listing_ref")
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
    # Names of the string-valued model inputs; every other input is cast to float64 (see _frame).
    categorical: Callable[[], list[str]] = field(default_factory=lambda: (lambda: []))


def _number(value: Any) -> float | None:
    if value is None or value != value:
        return None
    return float(value)


def _frame(inputs: dict[str, Any], columns: Sequence[str], categorical: Collection[str]) -> Any:
    """One-row model input frame, built as batch scoring builds it (``score_frame``).

    Columns in ``columns`` order; non-categorical columns go through ``to_numeric(errors="coerce")`` to float64
    (the model signature wants doubles, so None, Decimal and bool values become NaN or floats); categorical
    columns stay as they are.
    """
    import pandas as pd

    frame = pd.DataFrame([inputs]).reindex(columns=list(columns))
    for col in columns:
        if col not in categorical:
            frame[col] = pd.to_numeric(frame[col], errors="coerce").astype("float64")
    return frame.reset_index(drop=True)


def _slot_result(model: Any, frame: Any) -> dict[str, Any]:
    out = model.predict(frame).iloc[0]
    result: dict[str, Any] = {"label": str(out["model_version"])}
    for field in RESULT_FIELDS:
        result[field] = _number(out[field])
    result["floor_area_flagged"] = bool(out["floor_area_flagged"])
    return result


def _clean_block(joined: dict) -> dict[str, Any]:
    return {
        "record_id": joined.get("record_id"),
        "fetched_at": joined.get("fetched_at"),
        "trainable": joined.get("trainable"),
        "fx_rate_used": joined.get("fx_rate_used"),
        "price_jmd": joined.get("price_jmd"),
        "blocking_flags": blocking_flags(joined),
    }


def run(request: dict, deps: Deps) -> dict:
    site, ref = (request["key"][k] for k in KEY_FIELDS)
    columns = deps.columns()

    page: dict = {}
    page_error = None
    if request.get("snapshot_uri"):
        try:
            page = deps.fetch_page_fields(request["snapshot_uri"])
        except Exception as exc:  # noqa: BLE001 - reported, scoring continues without page fields
            page_error = str(exc)

    joined = deps.fetch_joined(site, ref)
    if joined is not None:
        input_source = "listing_clean"
        inputs = row_from_joined(joined, page, columns)
        clean = _clean_block(joined)
    else:
        input_source = "raw_fallback"
        geo, spatial = deps.fetch_geo_spatial(site, ref)
        inputs = row_from_raw(request.get("raw") or {}, geo, spatial, page, columns)
        clean = None

    frame = _frame(inputs, columns, set(deps.categorical()))
    slots: dict[str, Any] = {}
    for name, model_ref in (request.get("slots") or {}).items():
        if model_ref is None:
            continue
        try:
            slots[name] = _slot_result(deps.load_model(model_ref), frame)
        except Exception as exc:  # noqa: BLE001 - one failed slot must not stop the other
            slots[name] = {"error": str(exc)}

    return {
        "input_source": input_source,
        "clean": clean,
        "inputs": inputs,
        "slots": slots,
        "page_error": page_error,
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
        found = []
        for table in ("listing_geo_features", "listing_spatial_features"):
            found.append(
                select_one(
                    f"SELECT * FROM `{data.PROJECT}.{data.DATASET}.{table}` "
                    "WHERE source_site_id = @s AND site_listing_ref = @r",
                    site,
                    ref,
                )
            )
        return found[0], found[1]

    def fetch_page_fields(uri: str) -> dict:
        return page_fields.extract(field_check.fetch_page(uri, pages_dir))

    models: dict[str, Any] = {}

    def cached_model(ref: str) -> Any:
        if ref not in models:
            models[ref] = load_model(ref)
        return models[ref]

    return Deps(
        columns=bundle.input_columns,
        fetch_joined=fetch_joined,
        fetch_geo_spatial=fetch_geo_spatial,
        fetch_page_fields=fetch_page_fields,
        load_model=cached_model,
        categorical=bundle.categorical_inputs,
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
