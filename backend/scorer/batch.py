"""Bulk input rows and multi-row predictions for the batch view.

Runs under the ``ats_avm`` monorepo environment, inside the scorer server (``serve.py``). Pure given its ``Deps``,
so tests use fakes. Read only: BigQuery SELECTs and GCS reads.

Two steps, kept apart so the explorer can run them on different scorer processes:

* ``prepare_rows``: the model input row for every listing, with one bulk lookup per BigQuery table and the stored
  pages fetched in parallel. A listing that cannot be prepared gets an ``error``; it never stops the others.
* ``predict_rows``: one model, every prepared row, one ``predict`` call (so TabPFN pays its fixed cost once).
"""

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from scorer.score_listing import Deps, _frames, _row_result, assemble_row

PAGE_WORKERS = 16
LOOKUP_WORKERS = 8
Progress = Callable[[str, int, int], None]
Key = tuple[str, str]


def _key(item: dict) -> Key:
    return item["key"]["source_site_id"], item["key"]["site_listing_ref"]


def _message(exc: BaseException) -> str:
    return str(exc) or exc.__class__.__name__


def _no_progress(stage: str, done: int, total: int) -> None:
    return None


def _fetch_pages(uris: list[str], deps: Deps, workers: int, report: Progress) -> dict[str, tuple[dict, str | None]]:
    """{uri: (page fields, error)}; the stored pages are small, the cost is the round trip, so they run in parallel."""
    pages: dict[str, tuple[dict, str | None]] = {}
    report("pages", 0, len(uris))
    if not uris:
        return pages

    def one(uri: str) -> tuple[dict, str | None]:
        try:
            return deps.fetch_page_fields(uri), None
        except Exception as exc:  # noqa: BLE001 - reported on the row, scoring continues without page fields
            return {}, _message(exc)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(one, uri): uri for uri in uris}
        for done, future in enumerate(as_completed(futures), 1):
            pages[futures[future]] = future.result()
            report("pages", done, len(uris))
    return pages


def _lookup_joined(keys: list[Key], deps: Deps) -> dict[Key, Any]:
    """{key: listing_clean row, or the Exception raised for it}; a key that is not in listing_clean is absent."""
    if deps.fetch_joined_many is not None:
        return dict(deps.fetch_joined_many(keys))
    out: dict[Key, Any] = {}

    def one(key: Key) -> tuple[Key, Any]:
        try:
            return key, deps.fetch_joined(*key)
        except Exception as exc:  # noqa: BLE001 - becomes that listing's error
            return key, exc

    with ThreadPoolExecutor(max_workers=LOOKUP_WORKERS) as pool:
        for key, found in pool.map(one, keys):
            if found is not None:
                out[key] = found
    return out


def _lookup_geo(keys: list[Key], deps: Deps) -> dict[Key, Any]:
    """{key: (geo row or None, spatial row or None) or the Exception raised for it}."""
    if not keys:
        return {}
    if deps.fetch_geo_spatial_many is not None:
        return dict(deps.fetch_geo_spatial_many(keys))

    def one(key: Key) -> tuple[Key, Any]:
        try:
            return key, deps.fetch_geo_spatial(*key)
        except Exception as exc:  # noqa: BLE001 - becomes that listing's error
            return key, exc

    with ThreadPoolExecutor(max_workers=LOOKUP_WORKERS) as pool:
        return dict(pool.map(one, keys))


def prepare_rows(items: Sequence[dict], deps: Deps, progress: Progress | None = None,
                 page_workers: int = PAGE_WORKERS) -> list[dict]:
    """Input rows for ``items`` (each ``{"id", "key": {source_site_id, site_listing_ref}, "raw", "snapshot_uri"}``).

    Returns one dict per item, in order: ``id``, ``key``, ``input_source`` ("listing_clean" or "raw_fallback"),
    ``clean``, ``inputs``, ``page_error``, ``fx`` (a Bank of Jamaica rate for a USD listing outside listing_clean) and ``error``
    (set, with ``inputs`` None, when the row could not be built).
    ``progress(stage, done, total)`` is called for the stages "pages", "lookups" and "inputs".
    """
    report = progress or _no_progress
    items = list(items)
    total = len(items)
    if not total:
        return []
    columns = deps.columns()

    uris = list(dict.fromkeys(i["snapshot_uri"] for i in items if i.get("snapshot_uri")))
    pages = _fetch_pages(uris, deps, page_workers, report)

    report("lookups", 0, total)
    keys = list(dict.fromkeys(_key(i) for i in items))
    joined = _lookup_joined(keys, deps)
    geo = _lookup_geo([k for k in keys if k not in joined], deps)
    report("lookups", total, total)

    rows: list[dict] = []
    for done, item in enumerate(items, 1):
        key = _key(item)
        page, page_error = pages.get(item.get("snapshot_uri") or "", ({}, None))
        row = {"id": item["id"], "key": item["key"], "input_source": None, "clean": None, "inputs": None,
               "page_error": page_error, "error": None}
        try:
            found = joined.get(key)
            if isinstance(found, Exception):
                raise found
            geo_row = spatial_row = None
            if found is None:
                fallback = geo.get(key, (None, None))
                if isinstance(fallback, Exception):
                    raise fallback
                geo_row, spatial_row = fallback
            row.update(assemble_row(item.get("raw") or {}, found, geo_row, spatial_row, page, columns))
        except Exception as exc:  # noqa: BLE001 - this listing is reported as unscored, the others go on
            row["error"] = _message(exc)
        rows.append(row)
        report("inputs", done, total)
    _attach_fx(items, rows, deps)
    return rows


def _attach_fx(items: Sequence[dict], rows: list[dict], deps: Deps) -> None:
    """``fx`` ({"rate", "date"}) for each USD listing that is not in listing_clean, from one rates lookup: such a
    listing has no rate of its own, and without one it would have no JMD asking price and no gap. A failed lookup
    leaves ``fx`` None."""
    for row in rows:
        row["fx"] = None
    if deps.fx_rates is None:
        return
    wanted = [n for n, (item, row) in enumerate(zip(items, rows))
              if row["input_source"] == "raw_fallback" and (item.get("raw") or {}).get("currency") == "USD"]
    if not wanted:
        return
    try:
        found = deps.fx_rates([(items[n].get("raw") or {}).get("fetched_at") for n in wanted])
    except Exception:  # noqa: BLE001 - those listings just have no JMD asking price
        return
    for n, rate in zip(wanted, found):
        rows[n]["fx"] = rate


def _predict_isolating(model: Any, frame: Any, ids: list[str]) -> dict[str, dict]:
    """Predict every row in one call; if the model rejects the call, halve it until the bad rows stand alone."""
    try:
        out = model.predict(frame)
        if len(out) != len(ids):
            raise ValueError(f"the model returned {len(out)} rows for {len(ids)} listings")
    except Exception as exc:  # noqa: BLE001 - narrowed down below
        if len(ids) == 1:
            return {ids[0]: {"error": _message(exc)}}
        mid = len(ids) // 2
        results = _predict_isolating(model, frame.iloc[:mid].reset_index(drop=True), ids[:mid])
        results.update(_predict_isolating(model, frame.iloc[mid:].reset_index(drop=True), ids[mid:]))
        return results
    return {rid: _row_result(out.iloc[n]) for n, rid in enumerate(ids)}


def predict_rows(model_ref: str, rows: Sequence[dict], deps: Deps, progress: Progress | None = None) -> dict:
    """Score the prepared ``rows`` with one registry model: ``{"results": {id: slot result | {"error": ...}}}``.

    Rows without inputs (``prepare_rows`` could not build them) are left out of ``results``.
    """
    report = progress or _no_progress
    todo = [r for r in rows if r.get("inputs") is not None and not r.get("error")]
    if not todo:
        return {"results": {}}
    ids = [r["id"] for r in todo]
    report("model", 0, len(todo))
    try:
        model = deps.load_model(model_ref)
    except Exception as exc:  # noqa: BLE001 - every row reports why it has no estimate
        return {"results": {rid: {"error": _message(exc)} for rid in ids}}
    frame = _frames([r["inputs"] for r in todo], deps.columns(), set(deps.categorical()))
    report("predict", 0, len(todo))
    results = _predict_isolating(model, frame, ids)
    report("predict", len(todo), len(todo))
    return {"results": results}
