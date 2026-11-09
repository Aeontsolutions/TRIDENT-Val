"""ScorerLane against a scripted fake server (tests/fake_scorer_server.py): real processes, real pipes, no models."""
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app import scorer_client
from app.scorer_client import LaneSpec, ScorerCancelled, ScorerError, ScorerLane

FAKE = str(Path(__file__).parent / "fake_scorer_server.py")


def spec(signature="a"):
    return LaneSpec(cmd=[sys.executable, FAKE], env=dict(os.environ), signature=signature)


@pytest.fixture
def lane():
    state = {"signature": "a"}
    made = ScorerLane("test", lambda: spec(state["signature"]))
    made.state = state
    yield made
    made.stop()


def pid_alive(pid: int) -> bool:
    if os.name == "nt":  # os.kill(pid, 0) would terminate the process on Windows
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out.split()
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def wait_dead(pid: int) -> bool:
    for _ in range(50):
        if not pid_alive(pid):
            return True
        time.sleep(0.1)
    return False


def test_starts_lazily_and_keeps_one_process_between_calls(lane):
    assert lane.pid is None
    first = lane.call("echo", {"payload": 1}, timeout=30)
    second = lane.call("echo", {"payload": 2}, timeout=30)
    assert first["echo"] == 1 and second["echo"] == 2
    # on Windows the venv launcher is the lane's direct child, so only the server's own pid is compared
    assert first["pid"] == second["pid"] and lane.pid is not None


def test_progress_events_reach_the_callback_before_the_response(lane):
    events = []
    response = lane.call("events", timeout=30, on_event=events.append)
    assert response["ok"] is True
    assert [(e["stage"], e["done"], e["total"]) for e in events] == [("pages", 1, 3), ("pages", 2, 3), ("pages", 3, 3)]


def test_non_json_output_and_answers_to_old_requests_are_ignored(lane):
    assert lane.call("noise", timeout=30)["ok"] is True
    assert "stale" not in lane.call("stale", timeout=30)


def test_a_failed_request_raises_with_the_servers_message_and_keeps_the_process(lane):
    pid = lane.call("echo", timeout=30)["pid"]
    with pytest.raises(ScorerError, match="KeyError: bad listing"):
        lane.call("fatal", timeout=30)
    assert lane.call("echo", timeout=30)["pid"] == pid


def test_a_crash_raises_with_the_exit_code_and_stderr_and_the_next_call_restarts(lane):
    pid = lane.call("echo", timeout=30)["pid"]
    with pytest.raises(ScorerError) as caught:
        lane.call("crash", timeout=30)
    assert "exited with code 3" in str(caught.value) and "MemoryError" in str(caught.value)
    again = lane.call("echo", timeout=30)
    assert again["pid"] != pid and lane.pid is not None


def test_a_timeout_kills_the_process_and_the_next_call_starts_a_new_one(lane):
    pid = lane.call("echo", timeout=30)["pid"]
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        lane.call("slow", {"seconds": 30}, timeout=1)
    assert time.monotonic() - started < 10
    assert wait_dead(pid)
    assert lane.call("echo", timeout=30)["pid"] != pid


def test_a_changed_configuration_restarts_the_process(lane):
    pid = lane.call("echo", timeout=30)["pid"]
    lane.state["signature"] = "b"
    new = lane.call("echo", timeout=30)["pid"]
    assert new != pid and wait_dead(pid)


def test_cancel_aborts_the_call_in_flight(lane):
    result = {}

    def run():
        try:
            lane.call("slow", {"seconds": 30}, timeout=60)
        except Exception as exc:  # noqa: BLE001 - the test inspects it
            result["exc"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    for _ in range(100):  # wait until the request is in flight
        if lane.pid is not None:
            break
        time.sleep(0.05)
    time.sleep(0.5)
    pid = lane.pid
    started = time.monotonic()
    lane.cancel()
    thread.join(timeout=15)
    assert not thread.is_alive() and isinstance(result.get("exc"), ScorerCancelled)
    assert time.monotonic() - started < 10 and wait_dead(pid)
    assert lane.call("echo", timeout=30)["ok"] is True  # the cancel flag does not poison the next call


def test_a_flood_on_stderr_does_not_block_the_server(lane):
    assert lane.call("stderr_flood", timeout=30)["ok"] is True


def test_calls_on_one_lane_run_one_at_a_time(lane):
    lane.call("echo", timeout=30)  # start the process first so only the requests are timed
    tags = []

    def run(tag):
        tags.append(lane.call("slow", {"seconds": 0.6, "tag": tag}, timeout=30)["tag"])

    started = time.monotonic()
    threads = [threading.Thread(target=run, args=(t,)) for t in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(tags) == ["a", "b"] and time.monotonic() - started >= 1.1


def test_stop_ends_the_process_and_is_safe_to_repeat(lane):
    lane.stop()  # never started
    pid = lane.call("echo", timeout=30)["pid"]
    lane.stop()
    lane.stop()
    assert wait_dead(pid) and lane.pid is None
    assert lane.call("echo", timeout=30)["pid"] != pid  # and it can start again


def test_a_spec_that_cannot_be_built_raises_its_own_error():
    def broken():
        raise ScorerError("ATS AVM monorepo not found; set MODEL_CHECK_ATS_AVM_ROOT")

    with pytest.raises(ScorerError, match="MODEL_CHECK_ATS_AVM_ROOT"):
        ScorerLane("x", broken).call("echo", timeout=5)


def test_get_lane_returns_the_same_lane_for_a_name(monkeypatch):
    monkeypatch.setattr(scorer_client, "_lanes", {})
    first = scorer_client.get_lane("champion", spec)
    assert scorer_client.get_lane("champion", spec) is first
    assert scorer_client.get_lane("challenger", spec) is not first
    assert {lane.name for lane in scorer_client.known_lanes()} == {"champion", "challenger"}
