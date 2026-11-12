"""Long-lived scorer server: keeps the models, the BigQuery client and recent input rows between requests.

Run under the ats_avm monorepo environment, not the explorer's venv (``avm_model`` pins scikit-learn):

    uv run --package avm_model --extra prototype python tools/trident-val/backend/scorer/serve.py

The explorer's backend starts it (``backend/app/scorer_client.py``) and talks to it over stdin and stdout, one JSON
object per line, one request at a time:

    request   {"id": 1, "op": "score" | "prepare" | "predict" | "warm" | "explain" | "ping" | "shutdown", ...}
    event     {"id": 1, "event": "progress", "stage": "pages", "done": 10, "total": 120}   (zero or more)
    response  {"id": 1, "ok": true, ...}  or  {"id": 1, "ok": false, "fatal": "ValueError: ..."}

The server never exits on a failed request. Read only: BigQuery SELECTs, GCS reads and MLflow reads.
"""

import json
import sys
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from scorer import batch  # noqa: E402
from scorer.score_listing import Deps, _json_default, _real_deps, build_row, run  # noqa: E402


def dumps(message: dict) -> str:
    return json.dumps(message, default=_json_default)


def _op_score(msg: dict, deps: Deps, progress: batch.Progress) -> dict:
    return {"result": run(msg["request"], deps)}


def _op_prepare(msg: dict, deps: Deps, progress: batch.Progress) -> dict:
    return {"rows": batch.prepare_rows(msg["items"], deps, progress=progress)}


def _op_predict(msg: dict, deps: Deps, progress: batch.Progress) -> dict:
    return batch.predict_rows(str(msg["model"]), msg["rows"], deps, progress=progress)


def _op_warm(msg: dict, deps: Deps, progress: batch.Progress) -> dict:
    loaded: dict[str, dict] = {}
    for ref in msg.get("models") or []:
        started = time.perf_counter()
        try:
            deps.load_model(str(ref))
            loaded[str(ref)] = {"seconds": round(time.perf_counter() - started, 2)}
        except Exception as exc:  # noqa: BLE001 - reported per model; the server stays up
            loaded[str(ref)] = {"error": str(exc) or exc.__class__.__name__}
    return {"loaded": loaded}


def _op_explain(msg: dict, deps: Deps, progress: batch.Progress) -> dict:
    """Drivers and comps of one listing for one model: ``{"request": <score request>, "model": <version>}``."""
    if deps.explain is None:
        raise ValueError("this scorer has no explain support")
    request = msg["request"]
    built = build_row(request, deps)
    result = deps.explain(str(msg["model"]), built["inputs"], request.get("key") or {})
    return {"result": {**result, "input_source": built["input_source"], "page_error": built["page_error"]}}


OPS = {"score": _op_score, "prepare": _op_prepare, "predict": _op_predict, "warm": _op_warm, "explain": _op_explain}


def serve_loop(lines: Iterable[str], write: Callable[[dict], None], deps_factory: Callable[[], Deps]) -> None:
    """Answer requests from ``lines`` through ``write`` until ``shutdown`` or the end of the input.

    ``deps_factory`` runs on the first request that needs it (not for ``ping`` or ``shutdown``); if it raises, the
    request fails and the next request tries again."""
    deps: Deps | None = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            if not isinstance(msg, dict):
                raise ValueError("not an object")
        except ValueError:
            write({"id": None, "ok": False, "fatal": "The request is not a JSON object"})
            continue
        rid, op = msg.get("id"), msg.get("op")
        if op == "shutdown":
            write({"id": rid, "ok": True})
            return
        if op == "ping":
            import os

            write({"id": rid, "ok": True, "pid": os.getpid()})
            continue

        def progress(stage: str, done: int, total: int, rid: Any = rid) -> None:
            write({"id": rid, "event": "progress", "stage": stage, "done": done, "total": total})

        try:
            handler = OPS.get(op)
            if handler is None:
                raise ValueError(f"unknown op {op!r}")
            if deps is None:
                deps = deps_factory()
            write({"id": rid, "ok": True, **handler(msg, deps, progress)})
        except Exception as exc:  # noqa: BLE001 - one bad request must not take the server down
            write({"id": rid, "ok": False, "fatal": f"{type(exc).__name__}: {exc}"})


def main() -> int:
    stdout = sys.stdout
    sys.stdout = sys.stderr  # library chatter must not corrupt the response lines
    lock = threading.Lock()

    def write(message: dict) -> None:
        with lock:
            stdout.write(dumps(message) + "\n")
            stdout.flush()

    serve_loop(sys.stdin, write, _real_deps)
    return 0


if __name__ == "__main__":
    sys.exit(main())
