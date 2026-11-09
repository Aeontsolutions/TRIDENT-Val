"""Client for the long-lived scorer server (``scorer/serve.py``).

The server runs under the ``ats_avm`` monorepo environment (``avm_model`` pins scikit-learn), so it is a child process
and this module only moves JSON lines over its stdin and stdout. It must not import ``avm_model``, ``mlflow`` or
anything from the monorepo.

A ``ScorerLane`` is one such process. It starts on the first call, serves one request at a time, and is killed (with
its whole process tree) on a timeout, a cancel or a change of configuration. The next call starts a fresh one, so the
models it had loaded are loaded again.
"""
import atexit
import itertools
import json
import os
import queue
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from loguru import logger

STOP_WAIT_S = 3  # how long a clean shutdown may take before the process tree is killed
KILL_WAIT_S = 10
POLL_S = 0.5


class ScorerError(Exception):
    """The scorer failed or answered with something unusable. The message is shown to the user."""


class ScorerRequestFailed(ScorerError):
    """The server is fine but refused this request (for example the listing could not be turned into an input row)."""


class ScorerCancelled(ScorerError):
    """The call was cancelled with ``ScorerLane.cancel``."""


@dataclass(frozen=True)
class LaneSpec:
    cmd: list[str]
    env: dict[str, str]
    signature: Any  # compared between calls; a different value restarts the lane


def kill_tree(proc: subprocess.Popen) -> None:
    """Kill the child and everything it started. `uv run` launches python as a grandchild; killing only `uv` would
    leave that grandchild holding the pipes."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=10)
        else:
            os.killpg(proc.pid, signal.SIGKILL)  # the child leads its own session (start_new_session below)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def _tail(lines: deque, count: int = 2, limit: int = 300) -> str:
    kept = " | ".join(line.strip() for line in list(lines)[-count:] if line.strip())
    return kept[-limit:]


class ScorerLane:
    def __init__(self, name: str, spec_factory: Callable[[], LaneSpec]):
        self.name = name
        self._spec_factory = spec_factory
        self._lock = threading.Lock()  # one request at a time
        self._proc_lock = threading.Lock()  # guards the process handle; never held while waiting for a response
        self._proc: subprocess.Popen | None = None
        self._out: queue.Queue = queue.Queue()
        self._err: deque = deque(maxlen=40)
        self._signature: Any = None
        self._ids = itertools.count(1)
        self._cancelled = threading.Event()
        self._cmd: list[str] = []

    # ---------- process handling ----------

    @property
    def pid(self) -> int | None:
        proc = self._proc
        return proc.pid if proc is not None and proc.poll() is None else None

    def _start(self, spec: LaneSpec) -> None:
        proc = subprocess.Popen(spec.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", env=spec.env, start_new_session=os.name != "nt")
        out: queue.Queue = queue.Queue()
        err: deque = deque(maxlen=40)

        def read_stdout() -> None:
            for line in proc.stdout:  # type: ignore[union-attr]
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    err.append(line)  # library chatter that reached stdout; keep it for the error message
                    continue
                if isinstance(msg, dict):
                    out.put(msg)
            out.put(None)  # end of output: the process is gone

        def read_stderr() -> None:
            for line in proc.stderr:  # type: ignore[union-attr]
                err.append(line.rstrip("\n"))

        threading.Thread(target=read_stdout, name=f"scorer-{self.name}-out", daemon=True).start()
        threading.Thread(target=read_stderr, name=f"scorer-{self.name}-err", daemon=True).start()
        with self._proc_lock:
            self._proc, self._out, self._err = proc, out, err
            self._signature, self._cmd = spec.signature, spec.cmd
        logger.info("Scorer lane {n} started (pid {p})", n=self.name, p=proc.pid)

    def _kill(self) -> None:
        with self._proc_lock:
            proc, self._proc = self._proc, None
        if proc is None:
            return
        kill_tree(proc)
        try:
            proc.wait(timeout=KILL_WAIT_S)
        except subprocess.TimeoutExpired:
            pass

    def _ensure_running(self) -> None:
        spec = self._spec_factory()
        if self._proc is not None and (self._proc.poll() is not None or spec.signature != self._signature):
            if self._proc.poll() is None:
                logger.info("Scorer lane {n}: configuration changed, restarting", n=self.name)
            self._kill()
        if self._proc is None:
            self._start(spec)

    def cancel(self) -> None:
        """Abort the call in flight, if any: the process is killed and that call raises ScorerCancelled."""
        self._cancelled.set()
        self._kill()

    def stop(self) -> None:
        """Ask the server to exit, then kill whatever is left."""
        with self._proc_lock:
            proc = self._proc
        if proc is None:
            return
        try:
            proc.stdin.write(json.dumps({"id": 0, "op": "shutdown"}) + "\n")  # type: ignore[union-attr]
            proc.stdin.flush()  # type: ignore[union-attr]
            proc.wait(timeout=STOP_WAIT_S)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        self._kill()

    # ---------- calls ----------

    def call(self, op: str, payload: dict | None = None, *, timeout: float,
             on_event: Callable[[dict], None] | None = None) -> dict:
        """Send one request and return the response (a dict with ``ok`` true). ``on_event`` gets each progress event.

        Raises ScorerError (the server failed the request, died, or printed nothing usable), ScorerCancelled, and
        subprocess.TimeoutExpired (the process is killed, so the next call starts a fresh one)."""
        with self._lock:
            self._cancelled.clear()
            self._ensure_running()
            proc, out, err = self._proc, self._out, self._err
            rid = next(self._ids)
            try:
                proc.stdin.write(json.dumps({"id": rid, "op": op, **(payload or {})}) + "\n")  # type: ignore[union-attr]
                proc.stdin.flush()  # type: ignore[union-attr]
            except (OSError, ValueError) as exc:
                self._kill()
                raise ScorerError(f"The scorer is not accepting requests ({exc}); try again.") from exc
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._kill()
                    raise subprocess.TimeoutExpired(cmd=self.name, timeout=timeout)
                try:
                    msg = out.get(timeout=min(remaining, POLL_S))
                except queue.Empty:
                    continue
                if msg is None:
                    return self._died(proc, err)
                if msg.get("id") != rid:
                    continue  # an answer to an earlier, abandoned request
                if msg.get("event") == "progress":
                    if on_event is not None:
                        on_event(msg)
                    continue
                if msg.get("ok") is True:
                    return msg
                fatal = msg.get("fatal") or "the scorer reported a failure"
                raise ScorerRequestFailed(str(fatal))

    def _died(self, proc: subprocess.Popen, err: deque) -> dict:
        try:
            code = proc.wait(timeout=KILL_WAIT_S)
        except subprocess.TimeoutExpired:
            self._kill()
            code = proc.poll()
        with self._proc_lock:
            if self._proc is proc:
                self._proc = None
        if self._cancelled.is_set():
            raise ScorerCancelled("The scoring run was cancelled.")
        detail = _tail(err)
        raise ScorerError(f"The scorer exited with code {code}" + (f": {detail}" if detail else ""))


# ---------- the lanes of this backend ----------

_lanes: dict[str, ScorerLane] = {}
_lanes_lock = threading.Lock()


def get_lane(name: str, spec_factory: Callable[[], LaneSpec]) -> ScorerLane:
    with _lanes_lock:
        lane = _lanes.get(name)
        if lane is None:
            lane = _lanes[name] = ScorerLane(name, spec_factory)
        return lane


def known_lanes() -> list[ScorerLane]:
    with _lanes_lock:
        return list(_lanes.values())


def shutdown_all() -> None:
    for lane in known_lanes():
        try:
            lane.stop()
        except Exception as exc:  # noqa: BLE001 - shutting down; nothing useful to do with it
            logger.warning("Stopping scorer lane {n} failed: {e}", n=lane.name, e=exc)


atexit.register(shutdown_all)
