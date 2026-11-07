"""Model estimate for one scraped listing: what the registered listing-price model would say.

Read only. Registry reads go to the MLflow tracking server over HTTP; scoring runs in a subprocess under the
``ats_avm`` monorepo environment (``scorer/score_listing.py``), because ``avm_model`` pins its own scikit-learn.
This module runs in the explorer's venv and must not import ``avm_model``, ``mlflow`` or anything from the monorepo.

Env: ``MLFLOW_TRACKING_URI`` (default http://localhost:5000), ``MODEL_CHECK_ATS_AVM_ROOT`` (default: the ``ats_avm``
checkout that holds this submodule; must contain ``packages/avm_model``), ``MODEL_CHECK_TIMEOUT_S`` (default 180).
"""
import datetime as dt
import json
import math
import os
import signal
import subprocess
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel
from scorer.input_row import PROPERTY_TYPE_MAP, _text

from . import listings
from .auth import get_current_user
from .model_limits import flagged_fields, geo_missing, input_warnings, location_features_missing

router = APIRouter(prefix="/api/v1/model-check", tags=["model-check"], dependencies=[Depends(get_current_user)])

MODEL_NAME = "avm_listing_price"
SLOT_NAMES = ("champion", "challenger")
IN_SCOPE_LISTING_TYPE = "For Sale"  # as in avm_features listing_rules
UNSUPPORTED_MESSAGE = "Model estimates cover for-sale houses, apartments and townhouses only"
PROXY_HINT = "gcloud run services proxy mlflow --project ats-avm-jam --region us-central1 --port 5000"
SCORER_PATH = Path(__file__).resolve().parent.parent / "scorer" / "score_listing.py"
REGISTRY_TIMEOUT_S = 30  # the proxied registry has answered slowly now and then
KILL_DRAIN_TIMEOUT_S = 10  # how long to wait for the pipes to close after the scorer's process tree is killed
# Set by the venv launcher that started this backend; inherited by `uv run`, they break the scorer's own Python.
SCORER_ENV_DROP = ("PYTHONHOME", "PYTHONPATH", "UV_INTERNAL__PYTHONHOME", "VIRTUAL_ENV")
RESULT_FIELDS = (
    "pred_price_jmd",
    "lower_90_jmd",
    "upper_90_jmd",
    "lower_80_jmd",
    "upper_80_jmd",
    "lower_95_jmd",
    "upper_95_jmd",
)


class ScorerError(Exception):
    """The scorer subprocess failed or printed something unusable. The message is shown to the user."""


# ---------- pure helpers ----------

def is_supported(record: dict) -> tuple[bool, str | None]:
    """The model covers for-sale houses, apartments and townhouses. Same exact match as training: whitespace is
    trimmed and collapsed, case is not folded."""
    listing_type = record.get("listing_type_raw")
    if _text(listing_type) == IN_SCOPE_LISTING_TYPE and _text(record.get("property_type_raw")) in PROPERTY_TYPE_MAP:
        return True, None
    return False, (f"{UNSUPPORTED_MESSAGE} (this listing is {listing_type or 'of unknown type'} / "
                   f"{record.get('property_type_raw') or 'unknown property type'}).")


def asking_jmd(amount: float | None, currency: str | None, fx_rate_used: float | None) -> float | None:
    if amount is None:
        return None
    if currency == "JMD":
        return amount
    if currency == "USD" and fx_rate_used:
        return amount * fx_rate_used
    return None


def usd_rate(currency: str | None, clean: dict | None) -> tuple[float | None, bool]:
    """(rate for converting a USD on-screen amount, whether the cleaned row disagrees about the currency).

    listing_clean sets fx_rate_used = 1.0 for a JMD row, so the rate only applies when the cleaned row was priced in USD.
    """
    if currency != "USD" or clean is None:
        return None, False
    if clean.get("price_currency_original") == "USD":
        return clean.get("fx_rate_used"), False
    return None, True


def gap_pct(estimate: float, asking: float | None) -> float | None:
    """How far the estimate sits from the asking price, in percent of the asking price."""
    if estimate is None or asking is None or asking <= 0:
        return None
    return (estimate / asking - 1) * 100


def _no_nan(value: Any) -> Any:
    """JSON allows no NaN or Infinity; the stdlib parser accepts them, so turn them into null."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _no_nan(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_no_nan(v) for v in value]
    return value


# ---------- registry ----------

def _tracking_uri() -> str:
    return (os.environ.get("MLFLOW_TRACKING_URI") or "http://localhost:5000").rstrip("/")


def _registry_down_message(reason: Any) -> str:
    return (f"Model registry unreachable at {_tracking_uri()} ({reason}). "
            f"Open the proxy in another terminal: {PROXY_HINT}")


def _iso_from_ms(ms: Any) -> str:
    try:
        return dt.datetime.fromtimestamp(int(ms) / 1000, dt.timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def fetch_registry() -> tuple[list[dict], dict[str, str]]:
    """(versions newest first, alias -> version) of the registered model. Raises on any failure."""
    base = _tracking_uri() + "/api/2.0/mlflow"
    versions_resp = httpx.get(f"{base}/model-versions/search",
                              params={"filter": f"name='{MODEL_NAME}'", "max_results": 200},
                              timeout=REGISTRY_TIMEOUT_S)
    versions_resp.raise_for_status()
    model_resp = httpx.get(f"{base}/registered-models/get", params={"name": MODEL_NAME},
                            timeout=REGISTRY_TIMEOUT_S)
    model_resp.raise_for_status()
    aliases = {a["alias"]: str(a["version"])
               for a in (model_resp.json().get("registered_model") or {}).get("aliases") or []}
    versions = []
    for v in versions_resp.json().get("model_versions") or []:
        version = str(v["version"])
        versions.append({
            "version": version,
            "aliases": sorted(a for a, ver in aliases.items() if ver == version),
            "created_at": _iso_from_ms(v.get("creation_timestamp")),
        })
    versions.sort(key=lambda v: int(v["version"]), reverse=True)
    return versions, aliases


# ---------- scorer subprocess ----------

def _ats_avm_root() -> Path:
    env = os.environ.get("MODEL_CHECK_ATS_AVM_ROOT")
    return Path(env) if env else Path(__file__).resolve().parents[4]


def _timeout_s() -> float:
    try:
        return float(os.environ.get("MODEL_CHECK_TIMEOUT_S") or 180)
    except ValueError:
        return 180.0


def _tail(text: str, lines: int = 2, limit: int = 300) -> str:
    kept = " | ".join(line.strip() for line in (text or "").strip().splitlines()[-lines:] if line.strip())
    return kept[-limit:]


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill the child and everything it started. `uv run` launches python as a grandchild; killing only `uv` would leave
    that grandchild holding the output pipes, and the caller would wait until it finished anyway."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=10)
        else:
            os.killpg(proc.pid, signal.SIGKILL)  # the child leads its own session (start_new_session below)
    except (OSError, subprocess.SubprocessError):
        pass
    proc.kill()


def _run_child(cmd: list[str], *, input: str, timeout: float, env: dict) -> subprocess.CompletedProcess:
    """subprocess.run(..., capture_output=True, timeout=...) that kills the whole process tree on any exception
    (timeout, KeyboardInterrupt, a cancelled worker) and then re-raises it unchanged."""
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", env=env, start_new_session=os.name != "nt")
    try:
        stdout, stderr = proc.communicate(input, timeout=timeout)
    except BaseException:
        _kill_tree(proc)
        try:
            proc.communicate(timeout=KILL_DRAIN_TIMEOUT_S)  # reap the child and close the pipes
        except Exception:  # noqa: BLE001 - best effort; the original exception is what the caller needs
            pass
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def run_scorer(request: dict) -> dict:
    """Run the scorer under the monorepo env with ``request`` on stdin and return its parsed response.

    Raises ScorerError for a missing monorepo, a non-zero exit, empty or unparsable output; the caller also handles
    subprocess.TimeoutExpired and FileNotFoundError (uv not installed).
    """
    root = _ats_avm_root()
    if not (root / "packages" / "avm_model").is_dir():
        raise ScorerError("ATS AVM monorepo not found; set MODEL_CHECK_ATS_AVM_ROOT")
    cmd = ["uv", "run", "--frozen", "--project", str(root), "--package", "avm_model", "--extra", "prototype",
           "python", str(SCORER_PATH)]
    env = {k: v for k, v in os.environ.items() if k not in SCORER_ENV_DROP}
    env["MLFLOW_TRACKING_URI"] = _tracking_uri()
    done = _run_child(cmd, input=json.dumps(request), timeout=_timeout_s(), env=env)
    lines = [line for line in (done.stdout or "").splitlines() if line.strip()]
    payload = None
    if lines:
        try:
            payload = json.loads(lines[-1])
        except ValueError:
            payload = None
    if isinstance(payload, dict) and "fatal" in payload:
        raise ScorerError(f"The listing could not be turned into a model input row: {payload['fatal']}")
    if done.returncode != 0:
        detail = _tail(done.stderr)
        raise ScorerError(f"The scorer exited with code {done.returncode}" + (f": {detail}" if detail else ""))
    if not isinstance(payload, dict):
        detail = _tail(done.stderr)
        raise ScorerError("The scorer printed no usable result" + (f": {detail}" if detail else ""))
    return _no_nan(payload)


# ---------- endpoints ----------

class ScoreRequest(BaseModel):
    record_id: str
    champion: str = "champion"
    challenger: Optional[str] = "challenger"


@router.get("/versions")
def versions():
    try:
        found, aliases = fetch_registry()
    except Exception as exc:  # noqa: BLE001 - any registry failure is a 503 with instructions
        logger.warning("Model registry read failed: {e}", e=exc)
        raise HTTPException(503, _registry_down_message(exc))
    return {"model": MODEL_NAME, "versions": found, "aliases": aliases}


def _find_record(record_id: str) -> dict:
    for r in listings._rows():
        if r["record"]["record_id"] == record_id:
            return r
    raise HTTPException(404, "No such listing record")


def _resolve(requested: str | None, versions_found: list[dict], aliases: dict[str, str]) -> str | None:
    """A version number for an alias name or a version number that exists; None otherwise."""
    if not requested:
        return None
    if requested in aliases:
        return aliases[requested]
    if requested.isdigit() and any(v["version"] == str(int(requested)) for v in versions_found):
        return str(int(requested))
    return None


def _slot_out(scored: dict, asking: float | None) -> dict:
    if scored.get("error") is not None:
        return {"status": "error", "message": str(scored["error"])}
    out = {"status": "ok", "label": scored.get("label")}
    for name in RESULT_FIELDS:
        out[name] = scored.get(name)
    out["gap_pct"] = gap_pct(scored.get("pred_price_jmd"), asking)
    out["floor_area_flagged"] = bool(scored.get("floor_area_flagged"))
    return out


def _warnings(result: dict, record_id: str, inputs: dict, slots: dict, currency: str | None) -> list[dict]:
    out: list[dict] = []
    clean = result.get("clean")
    if clean is None:
        message = "This listing is not in the clean table; the inputs were built from the raw scrape."
        if currency != "JMD":
            message += (" There is no JMD asking price or gap, because the exchange rate comes from the cleaned "
                        "table.")
        out.append({"code": "not_in_clean_table", "message": message})
    else:
        if clean.get("record_id") not in (None, record_id):
            out.append({"code": "observation_differs",
                        "message": f"The clean table holds a different observation of this listing "
                                   f"({clean['record_id']}) than the one on screen ({record_id}); "
                                   f"the cleaned fields come from {clean['record_id']}, the page fields from the "
                                   f"observation on screen."})
        if clean.get("trainable") is True:
            out.append({"code": "probably_in_training",
                        "message": "This listing is in the clean table and marked trainable, so it was probably in "
                                   "the training data (a share is held back as a locked test set, so it may not "
                                   "have been); this is not an out-of-sample check."})
        elif clean.get("trainable") is False:
            flags = ", ".join(clean.get("blocking_flags") or []) or "none recorded"
            out.append({"code": "blocked_from_training",
                        "message": f"This listing is in the clean table but excluded from training "
                                   f"(blocking flags: {flags})."})
    if usd_rate(currency, clean)[1]:
        was = clean.get("price_currency_original")
        out.append({"code": "currency_differs",
                    "message": "The observation on screen is priced in USD but listing_clean's "
                               + (f"is in {was}" if was else "has no original currency")
                               + "; no JMD asking price or gap is shown."})
    out += [{"code": w["code"], "message": w["message"]} for w in input_warnings(inputs)]
    if any(s.get("status") == "ok" and s.get("floor_area_flagged") for s in slots.values()):
        out.append({"code": "floor_area_ignored",
                    "message": "The model judged the floor area implausible and ignored it in at least one estimate."})
    if geo_missing(inputs):
        out.append({"code": "geo_missing",
                    "message": "Latitude or longitude is missing, so location features are filled in as unknown."})
    elif location_features_missing(inputs):
        out.append({"code": "location_features_missing",
                    "message": "Location features (distance to coast, elevation, nearby schools/roads ...) were not "
                               "computed for this listing; the estimate leans on the other fields."})
    if result.get("page_error"):
        out.append({"code": "page_fields_unavailable",
                    "message": f"Fields read from the listing page were unavailable: {result['page_error']}"})
    return out


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _scorer_failure_message(exc: Exception) -> str:
    if isinstance(exc, subprocess.TimeoutExpired):
        return (f"The scorer did not finish within {_timeout_s():g} s (the first run downloads the model); "
                "try again.")
    if isinstance(exc, FileNotFoundError):
        return "uv was not found on PATH; install uv to run the scorer."
    return str(exc) or exc.__class__.__name__


@router.post("/score")
def score(body: ScoreRequest):
    record = _find_record(body.record_id)
    item = listings._list_item(record)
    supported, reason = is_supported(item)
    asking = {"amount": item["amount"], "currency": item["currency"],
              "jmd": asking_jmd(item["amount"], item["currency"], None)}
    out: dict[str, Any] = {"supported": supported, "unsupported_reason": reason, "asking": asking,
                           "input_source": None, "clean": None, "inputs": [], "slots": {},
                           "warnings": [], "registry_error": None, "scored_at": _now_iso()}
    requested = {"champion": body.champion, "challenger": body.challenger}
    if not supported:
        out["slots"] = {name: {"status": "skipped"} for name in SLOT_NAMES}
        return out

    versions_found: list[dict] = []
    aliases: dict[str, str] = {}
    try:
        versions_found, aliases = fetch_registry()
    except Exception as exc:  # noqa: BLE001 - recorded; the inputs are still worth showing
        logger.warning("Model registry read failed: {e}", e=exc)
        out["registry_error"] = _registry_down_message(exc)
    resolved = {n: None if out["registry_error"] else _resolve(requested[n], versions_found, aliases)
                for n in SLOT_NAMES}

    request = {
        "key": {"source_site_id": listings._g(record, "provenance", "source_site_id"),
                "site_listing_ref": listings._g(record, "provenance", "site_listing_ref")},
        "raw": item,
        "snapshot_uri": listings._g(record, "provenance", "snapshot_uri"),
        "slots": resolved,
    }
    result: dict | None = None
    failure: str | None = None
    try:
        result = run_scorer(request)
    except (ScorerError, subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError) as exc:
        failure = _scorer_failure_message(exc)
        logger.warning("Model-check scorer failed for {r}: {m}", r=body.record_id, m=failure)

    scored_slots = (result or {}).get("slots") or {}
    clean = (result or {}).get("clean")
    fx, _ = usd_rate(item["currency"], clean)
    asking["jmd"] = asking_jmd(item["amount"], item["currency"], fx)

    slots: dict[str, dict] = {}
    for name in SLOT_NAMES:
        if requested[name] is None:
            slots[name] = {"status": "skipped"}
        elif out["registry_error"]:
            slots[name] = {"status": "error", "message": out["registry_error"]}
        elif resolved[name] is None:
            slots[name] = {"status": "not_registered"}
        elif failure is not None:
            slots[name] = {"status": "error", "message": failure}
        elif name in scored_slots:
            slots[name] = _slot_out(scored_slots[name], asking["jmd"])
        else:
            slots[name] = {"status": "error", "message": "The scorer returned no result for this slot."}
    out["slots"] = slots

    if result is not None:
        inputs = result.get("inputs") or {}
        flags = flagged_fields(inputs)
        out["input_source"] = result.get("input_source")
        out["clean"] = clean
        out["inputs"] = [{"name": k, "value": v, "flag": flags.get(k)} for k, v in inputs.items()]
        out["warnings"] = _warnings(result, body.record_id, inputs, slots, item["currency"])
    out["scored_at"] = _now_iso()
    return out
