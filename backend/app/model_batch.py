"""Batch model estimate: every new listing of the latest scrape run, scored with the champion and the challenger.

"New" means the listing appears in the latest run and in no earlier run (``listings.latest_run_new_listings``). The
batch is started by hand, runs in a background thread, and its result lives in memory only (one job at a time).

Like ``model_check`` this runs in the explorer's venv and only talks to the scorer processes: the champion lane builds
the input rows for all listings in one bulk pass (``prepare``), then each slot's lane predicts them in one call
(``predict``), in parallel. TabPFN's cost is almost all fixed per call, which is why the challenger is scored on every
row at once instead of one listing at a time. Read only; nothing is written to BigQuery, GCS or the registry.

Env: ``MODEL_CHECK_BATCH_TIMEOUT_S`` (default 3600), the limit for each scorer request of a batch.
"""
import statistics
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel, Field

from . import listings, model_check
from .auth import get_current_user
from .model_limits import flagged_fields, geo_missing, input_warnings, location_features_missing
from .scorer_client import ScorerCancelled

router = APIRouter(prefix="/api/v1/model-check/batch", tags=["model-check"],
                   dependencies=[Depends(get_current_user)])

GAP_FLAG_PCT = 25.0  # rows whose estimate is further than this from the asking price are highlighted
BATCH_TIMEOUT_S = 3600.0
SLOTS = model_check.SLOT_NAMES


def _batch_timeout_s() -> float:
    import os

    try:
        return float(os.environ.get("MODEL_CHECK_BATCH_TIMEOUT_S") or BATCH_TIMEOUT_S)
    except ValueError:
        return BATCH_TIMEOUT_S


# ---------- pure helpers ----------

def select_batch(limit: Optional[int] = None) -> dict:
    """The latest run's new listings split into ``todo`` ([(record, list item)] the model covers) and ``unscored``
    (the rest, each with its reason)."""
    sel = listings.latest_run_new_listings(listings._rows())
    if sel is None:
        raise HTTPException(404, "There are no scrape runs to score yet")
    todo: list[tuple[dict, dict]] = []
    unscored: list[dict] = []
    for record in sel["new"]:
        item = listings._list_item(record)
        supported, reason = model_check.is_supported(item)
        if supported:
            todo.append((record, item))
        else:
            unscored.append(_identity(item) | {"reason": reason})
    scored_total = len(todo)
    if limit is not None:
        todo = todo[:limit]
    run = {"run_id": sel["run_id"], "first_fetched_at": sel["first_fetched_at"], "earlier_runs": sel["earlier_runs"],
           "listings_in_run": sel["listings_in_run"], "new_listings": len(sel["new"]),
           "unkeyed_rows": sel["unkeyed_rows"], "supported": scored_total, "limit": limit}
    return {"run": run, "todo": todo, "unscored": unscored}


def _identity(item: dict) -> dict:
    return {"record_id": item["record_id"], "ref": item["site_listing_ref"], "title": item["title"],
            "property_type": item["property_type_raw"], "listing_type": item["listing_type_raw"],
            "parish": item["parish_raw"], "asking": {"amount": item["amount"], "currency": item["currency"]}}


def _row_flags(inputs: dict) -> list[str]:
    flags = [f"{name} {kind}" for name, kind in flagged_fields(inputs).items() if kind]
    flags += [w["code"] for w in input_warnings(inputs) if w["code"] != "input_out_of_range"]
    if geo_missing(inputs):
        flags.append("geo_missing")
    elif location_features_missing(inputs):
        flags.append("location_features_missing")
    return flags


def large_gap(row: dict) -> bool:
    return any(abs(s["gap_pct"]) > GAP_FLAG_PCT for s in row["slots"].values()
               if s.get("status") == "ok" and s.get("gap_pct") is not None)


def _median(values: list[float]) -> Optional[float]:
    return statistics.median(values) if values else None


def summarize(rows: list[dict]) -> dict:
    """Median gap to asking by property type and by parish, so a pattern stands out from one-off listings."""

    def group(getter) -> list[dict]:
        groups: dict[str, list[dict]] = {}
        for row in rows:
            groups.setdefault(getter(row) or "(unknown)", []).append(row)
        out = []
        for value, members in groups.items():
            entry: dict[str, Any] = {"value": value, "count": len(members)}
            for name in SLOTS:
                gaps = [m["slots"][name]["gap_pct"] for m in members
                        if m["slots"].get(name, {}).get("status") == "ok" and m["slots"][name].get("gap_pct") is not None]
                entry[f"{name}_median_gap_pct"] = _median(gaps)
                entry[f"{name}_gap_count"] = len(gaps)
            entry["large_gap_count"] = sum(1 for m in members if large_gap(m))
            out.append(entry)
        return sorted(out, key=lambda e: (-e["count"], e["value"]))

    overall: dict[str, Any] = {"count": len(rows), "large_gap_count": sum(1 for r in rows if large_gap(r))}
    for name in SLOTS:
        gaps = [r["slots"][name]["gap_pct"] for r in rows
                if r["slots"].get(name, {}).get("status") == "ok" and r["slots"][name].get("gap_pct") is not None]
        overall[f"{name}_median_gap_pct"] = _median(gaps)
        overall[f"{name}_gap_count"] = len(gaps)
    return {"overall": overall, "by_property_type": group(lambda r: r["property_type"]),
            "by_parish": group(lambda r: r.get("parish_input") or r["parish"])}


# ---------- the job ----------

class Job:
    def __init__(self, run: dict, rows: list[dict], unscored: list[dict]):
        self.id = uuid.uuid4().hex[:12]
        self.lock = threading.Lock()
        self.started = time.time()
        self.finished: Optional[float] = None
        self.status = "running"  # running | done | cancelled | error
        self.stage = "starting"
        self.message: Optional[str] = None
        self.run = run
        self.rows: dict[str, dict] = {r["record_id"]: r for r in rows}
        self.unscored = unscored
        self.rows_version = 1
        self.progress: dict[str, dict] = {}
        self.slot_state: dict[str, dict] = {n: {"state": "waiting", "version": None, "started_at": None,
                                                "finished_at": None, "message": None} for n in SLOTS}
        self.cancelled = threading.Event()
        self.thread: Optional[threading.Thread] = None

    # -- updates (all under the lock) --

    def set_stage(self, stage: str) -> None:
        with self.lock:
            self.stage = stage

    def set_progress(self, key: str, stage: str, done: int, total: int) -> None:
        with self.lock:
            self.progress[key] = {"stage": stage, "done": done, "total": total}

    def set_slot(self, name: str, **fields: Any) -> None:
        with self.lock:
            self.slot_state[name].update(fields)

    def touch_rows(self) -> None:
        self.rows_version += 1

    def finish(self, status: str, message: Optional[str] = None) -> None:
        with self.lock:
            self.status, self.message, self.finished = status, message, time.time()
            self.stage = "finished"
            self.rows_version += 1

    # -- reading --

    def snapshot(self, since: Optional[int]) -> dict:
        with self.lock:
            now = self.finished or time.time()
            out: dict[str, Any] = {
                "id": self.id, "status": self.status, "stage": self.stage, "message": self.message,
                "started_at": self.started, "elapsed_s": round(now - self.started, 1), "run": self.run,
                "progress": {k: dict(v) for k, v in self.progress.items()},
                "slots": {n: dict(s) for n, s in self.slot_state.items()},
                "gap_threshold_pct": GAP_FLAG_PCT, "rows_version": self.rows_version,
                "counts": {"scoring": len(self.rows), "unscored": len(self.unscored)},
            }
            if since != self.rows_version:
                rows = [dict(r, large_gap=large_gap(r)) for r in self.rows.values()]
                out["rows"] = rows
                out["unscored"] = list(self.unscored)
                out["summary"] = summarize(rows)
            return out


_current: Optional[Job] = None
_current_lock = threading.Lock()


def current_job() -> Optional[Job]:
    return _current


def wait_for_job(timeout: float = 30.0) -> None:
    """Block until the current job's thread ends (tests)."""
    job = _current
    if job is not None and job.thread is not None:
        job.thread.join(timeout)


def _check_cancelled(job: Job) -> None:
    """A cancel that lands between two scorer calls would otherwise be undone by the next call starting a fresh process."""
    if job.cancelled.is_set():
        raise ScorerCancelled("The batch was cancelled.")


def _failure(exc: BaseException) -> str:
    return model_check._lane_failure_message(exc)


def _apply_prepared(job: Job, prepared: list[dict]) -> list[dict]:
    """Fill the rows from the scorer's prepared inputs; rows it could not prepare move to the unscored list.
    Returns the rows that go on to prediction."""
    ready: list[dict] = []
    with job.lock:
        for p in prepared:
            row = job.rows.get(p["id"])
            if row is None:
                continue
            if p.get("error") or p.get("inputs") is None:
                job.unscored.append({k: row[k] for k in ("record_id", "ref", "title", "property_type", "listing_type",
                                                         "parish", "asking")}
                                    | {"reason": f"The model inputs could not be built: {p.get('error')}"})
                del job.rows[p["id"]]
                continue
            inputs = p["inputs"]
            fx, _ = model_check.usd_rate(row["asking"]["currency"], p.get("clean"))
            boj = p.get("fx") if fx is None and row["asking"]["currency"] == "USD" else None
            if boj:  # not in the clean table: the Bank of Jamaica rate for its fetch date, as listing_clean would use
                fx = boj["rate"]
                row["asking"]["fx"] = {"rate": boj["rate"], "date": boj["date"]}
            row["asking"]["jmd"] = model_check.asking_jmd(row["asking"]["amount"], row["asking"]["currency"], fx)
            row["input_source"] = p.get("input_source")
            row["page_error"] = bool(p.get("page_error"))
            row["flags"] = _row_flags(inputs)
            row["bedrooms"], row["bathrooms"] = inputs.get("bedrooms"), inputs.get("bathrooms")
            row["parish_input"] = inputs.get("parish")
            ready.append({"id": p["id"], "inputs": inputs})
        job.touch_rows()
    return ready


def _merge_results(job: Job, name: str, results: dict[str, dict]) -> None:
    with job.lock:
        for rid, row in job.rows.items():
            scored = results.get(rid)
            if scored is None:
                row["slots"][name] = {"status": "error", "message": "The scorer returned no result for this listing."}
            else:
                row["slots"][name] = model_check._slot_out(scored, row["asking"].get("jmd"))
        job.touch_rows()


def _fail_slot(job: Job, name: str, message: str) -> None:
    with job.lock:
        for row in job.rows.values():
            row["slots"][name] = {"status": "error", "message": message}
        job.touch_rows()
    job.set_slot(name, state="error", finished_at=time.time(), message=message)


def _predict_slot(job: Job, name: str, version: str, ready: list[dict]) -> None:
    job.set_slot(name, state="running", version=version, started_at=time.time())

    def on_event(event: dict) -> None:
        job.set_progress(name, event["stage"], event["done"], event["total"])

    try:
        msg = model_check.lane_for(name).call("predict", {"model": version, "rows": ready},
                                              timeout=_batch_timeout_s(), on_event=on_event)
    except ScorerCancelled:
        raise
    except Exception as exc:  # noqa: BLE001 - this slot fails for every row; the other slot is unaffected
        logger.warning("Batch {n} prediction failed: {e}", n=name, e=_failure(exc))
        _fail_slot(job, name, _failure(exc))
        return
    _merge_results(job, name, model_check._no_nan(msg.get("results") or {}))
    job.set_slot(name, state="done", finished_at=time.time())


def _run(job: Job, items: list[dict], requested: dict[str, Optional[str]]) -> None:
    try:
        job.set_stage("registry")
        try:
            found, aliases = model_check.registry()
        except Exception as exc:  # noqa: BLE001 - reported as the job's error
            job.finish("error", model_check._registry_down_message(exc))
            return
        versions: dict[str, str] = {}
        for name in SLOTS:
            if requested[name] is None:
                job.set_slot(name, state="skipped")
                _set_slot_everywhere(job, name, {"status": "skipped"})
                continue
            version = model_check._resolve(requested[name], found, aliases)
            if version is None:
                job.set_slot(name, state="not_registered")
                _set_slot_everywhere(job, name, {"status": "not_registered"})
            else:
                versions[name] = version
        if not versions:
            job.finish("error", "No model to score with: pick a champion or a challenger that is registered.")
            return

        _check_cancelled(job)
        job.set_stage("prepare")
        lane = model_check.lane_for("champion")
        prepared = lane.call("prepare", {"items": items}, timeout=_batch_timeout_s(),
                             on_event=lambda e: job.set_progress("prepare", e["stage"], e["done"], e["total"]))["rows"]
        ready = _apply_prepared(job, prepared)

        _check_cancelled(job)
        job.set_stage("scoring")
        with ThreadPoolExecutor(max_workers=len(versions)) as pool:
            futures = [pool.submit(_predict_slot, job, name, version, ready) for name, version in versions.items()]
            for future in futures:
                future.result()
        job.finish("done")
    except ScorerCancelled:
        job.finish("cancelled", "The batch was cancelled. The scorer processes were stopped, so the models load again "
                                "on the next run.")
    except Exception as exc:  # noqa: BLE001 - the job's error, shown in the UI
        logger.warning("Batch run failed: {e}", e=_failure(exc))
        if job.cancelled.is_set():
            job.finish("cancelled", "The batch was cancelled.")
        else:
            job.finish("error", _failure(exc))


def _set_slot_everywhere(job: Job, name: str, value: dict) -> None:
    with job.lock:
        for row in job.rows.values():
            row["slots"][name] = dict(value)
        job.touch_rows()


# ---------- endpoints ----------

class BatchRequest(BaseModel):
    champion: Optional[str] = "champion"
    challenger: Optional[str] = "challenger"
    limit: Optional[int] = Field(None, ge=1, description="Score only the first N supported listings (a quick trial)")


def _start(body: BatchRequest) -> Job:
    global _current
    with _current_lock:
        if _current is not None and _current.status == "running":
            raise HTTPException(409, "A batch is already running. Cancel it first, or wait for it to finish.")
        chosen = select_batch(body.limit)
        rows, items = [], []
        for record, item in chosen["todo"]:
            row = _identity(item) | {
                "input_source": None, "page_error": False, "flags": [], "bedrooms": None, "bathrooms": None,
                "parish_input": None, "slots": {n: {"status": "pending"} for n in SLOTS}}
            row["asking"]["jmd"] = None
            rows.append(row)
            items.append({"id": item["record_id"],
                          "key": {"source_site_id": listings._g(record, "provenance", "source_site_id"),
                                  "site_listing_ref": listings._g(record, "provenance", "site_listing_ref")},
                          "raw": item, "snapshot_uri": listings._g(record, "provenance", "snapshot_uri")})
        job = Job(chosen["run"], rows, chosen["unscored"])
        job.thread = threading.Thread(target=_run, args=(job, items, {"champion": body.champion,
                                                                       "challenger": body.challenger}),
                                      name="model-batch", daemon=True)
        _current = job
        job.thread.start()
        return job


@router.post("")
def start_batch(body: BatchRequest):
    return _start(body).snapshot(None)


@router.get("")
def get_batch(since: Optional[int] = None):
    job = _current
    if job is None:
        return {"job": None}
    return job.snapshot(since)


@router.post("/cancel")
def cancel_batch():
    job = _current
    if job is None or job.status != "running":
        return {"cancelled": False}
    job.cancelled.set()
    for name in SLOTS:
        try:
            model_check.lane_for(name).cancel()
        except Exception as exc:  # noqa: BLE001 - best effort; the job thread ends on the first cancelled call
            logger.warning("Cancelling scorer lane {n} failed: {e}", n=name, e=exc)
    return {"cancelled": True}
