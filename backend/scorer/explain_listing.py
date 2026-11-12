"""Drivers (TreeSHAP) and comparable listings for one listing, for the Model estimate panel.

Monorepo side, like the rest of ``backend/scorer``: it runs in the scorer process (``serve.py``, op ``explain``) and
imports ``avm_model`` only inside ``make_explain``'s function, never at module import. The work is in
``avm_model.explain`` and ``avm_model.comps``; this module only caches and serialises.

The comps pool is a parquet of listing rows (raw model inputs, asking price, status, dates, keys) written by
``scripts/explain/example_report.py --write-pool`` in the ats_avm checkout; ``MODEL_CHECK_COMPS_POOL`` points at it
(the explorer's backend sets it to ``<ats_avm root>/data/explain/comps_pool.parquet`` unless given). Without the file
the panel shows the drivers and says how to make the pool. Every price is an asking price.
"""

import math
import os
import threading
import time
from collections.abc import Callable, Collection, Sequence
from pathlib import Path
from typing import Any

COMPS_POOL_ENV = "MODEL_CHECK_COMPS_POOL"
POOL_HINT = ("No comps pool: run `uv run --package avm_model --extra prototype python scripts/explain/example_report.py "
             "--write-pool data/explain/comps_pool.parquet` in the ats_avm checkout (or set MODEL_CHECK_COMPS_POOL).")
KEY_FIELDS = ("source_site_id", "site_listing_ref")


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def _text(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    return str(v)


def drivers_payload(exp: Any, column_label: Callable[[str], str]) -> list[dict]:
    """One subject's drivers, largest first, as JSON-able rows."""
    out = []
    for _, r in exp.drivers(0).iterrows():
        out.append({
            "group": r["group"],
            "pct_effect": _num(r["pct_effect"]),
            "log_effect": _num(r["log_effect"]),
            "value_text": r["value_text"] or "",
            "top": [{"input": c, "label": column_label(c), "pct_effect": _num(p)} for c, _, p in r["top_columns"]],
        })
    return out


def comps_payload(res: Any) -> dict:
    comps = []
    for _, c in res.comps.iterrows():
        date = c["listing_date"]
        comps.append({
            "comp": int(c["comp"]),
            "key": c["key"],
            "listing_url": _text(c["listing_url"]),
            "listing_status": _text(c["listing_status"]),
            "listing_date": None if date is None or date != date else str(date)[:10],
            "community": _text(c["community"]),
            "bedrooms": _num(c["bedrooms"]),
            "bathrooms": _num(c["bathrooms"]),
            "floor_area_sqft": _num(c["floor_area_sqft"]),
            "same_building": bool(c["same_building"]),
            "distance_km": _num(c["distance_km"]),
            "proximity": _num(c["proximity"]),
            "match": c["match"],
            "price_jmd": _num(c["price_jmd"]),
            "adjustment_pct": _num(c["adjustment_pct"]),
            "adjusted_jmd": _num(c["adjusted_jmd"]),
            "gross_adjustment_pct": _num(c["gross_adjustment_pct"]),
        })
    grid = [{"comp": int(g["comp"]), "group": g["group"], "pct_effect": _num(g["pct_effect"]), "jmd": _num(g["jmd"])}
            for _, g in res.grid.iterrows()]
    return {"available": True, "comps": comps, "grid": grid, "candidates": int(res.candidates),
            "indicated_jmd": _num(res.indicated_jmd), "notes": list(res.notes)}


def make_explain(load_model: Callable[[str], Any], columns: Callable[[], list[str]],
                 categorical: Callable[[], list[str]],
                 frames: Callable[[Sequence[dict], Sequence[str], Collection[str]], Any]) -> Callable[..., dict]:
    """``explain(model_ref, inputs, key) -> dict`` with one explainer per model and one comps pool per (model, pool
    file), built on first use and kept for the life of the scorer process."""
    explainers: dict[str, Any] = {}
    pools: dict[tuple, Any] = {}
    lock = threading.Lock()

    def explainer_for(ref: str) -> Any:
        from avm_model.explain import BundleExplainer

        with lock:
            if ref not in explainers:
                explainers[ref] = BundleExplainer(load_model(ref).unwrap_python_model().bundle)
            return explainers[ref]

    def pool_for(ref: str, explainer: Any) -> tuple[Any, str | None]:
        path = Path(os.environ.get(COMPS_POOL_ENV) or "")
        if not str(path) or not path.is_file():
            return None, POOL_HINT
        sig = (ref, str(path), path.stat().st_mtime)
        with lock:
            if sig not in pools:
                import pandas as pd
                from avm_model.comps import CompPool

                pools[sig] = CompPool(pd.read_parquet(path), explainer)
            return pools[sig], None

    def explain(model_ref: str, inputs: dict, key: dict) -> dict:
        from avm_model.comps import find_comps
        from avm_model.explain import column_label

        seconds: dict[str, float] = {}
        t = time.perf_counter()
        explainer = explainer_for(str(model_ref))
        seconds["load"] = time.perf_counter() - t
        frame = frames([inputs], columns(), set(categorical()))
        for k in KEY_FIELDS:
            frame[k] = (key or {}).get(k)
        t = time.perf_counter()
        exp = explainer.explain(frame)
        seconds["drivers"] = time.perf_counter() - t
        out: dict[str, Any] = {
            "point_model": explainer.bundle.point_model,
            "base_jmd": math.exp(exp.base),
            "pred_jmd": math.exp(float(exp.pred_log[0])),
            "check_gap": float(exp.max_gap),
            "drivers": drivers_payload(exp, column_label),
        }
        t = time.perf_counter()
        pool, why = pool_for(str(model_ref), explainer)
        if pool is None:
            out["comps"] = {"available": False, "message": why}
        else:
            out["comps"] = comps_payload(find_comps(frame, pool))
        seconds["comps"] = time.perf_counter() - t
        out["seconds"] = {k: round(v, 2) for k, v in seconds.items()}
        return out

    return explain
