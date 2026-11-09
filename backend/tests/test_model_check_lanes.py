"""model_check in persistent mode: one scorer lane per slot, the registry cache and the warm-up. Fake lanes only."""
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import listings, model_check
from app.auth import get_current_user
from app.scorer_client import ScorerError, ScorerRequestFailed

FIXTURE = Path(__file__).parent / "fixtures" / "model_check_sample.ndjson"
VERSIONS = [{"version": "2", "aliases": ["challenger"], "created_at": ""},
            {"version": "1", "aliases": ["champion"], "created_at": ""}]
ALIASES = {"champion": "1", "challenger": "2"}


def slot(price, label):
    return {"label": label, "pred_price_jmd": price, "lower_80_jmd": price, "upper_80_jmd": price,
            "lower_90_jmd": price, "upper_90_jmd": price, "lower_95_jmd": price, "upper_95_jmd": price,
            "floor_area_flagged": False}


class FakeLane:
    def __init__(self, name, outcome=None, pid=100):
        self.name, self.outcome, self.pid, self.calls = name, outcome, pid, []

    def call(self, op, payload=None, *, timeout, on_event=None):
        self.calls.append((op, payload, timeout))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        if op == "warm":
            return {"ok": True, "loaded": {}}
        (name, ref), = payload["request"]["slots"].items() or [(None, None)]
        result = {"input_source": "raw_fallback", "clean": None, "inputs": {"bedrooms": 3.0, "latitude": 18.0,
                  "longitude": -77.0}, "page_error": None,
                  "slots": {name: slot(1e6 * int(ref), f"m/{ref}")} if name else {}}
        return {"ok": True, "result": result}


@pytest.fixture
def lanes(monkeypatch, tmp_path):
    (tmp_path / "packages" / "avm_model").mkdir(parents=True)
    monkeypatch.setenv("MODEL_CHECK_ATS_AVM_ROOT", str(tmp_path))
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://reg:5000")
    monkeypatch.delenv("MODEL_CHECK_SCORER_MODE", raising=False)
    monkeypatch.delenv("MODEL_CHECK_TIMEOUT_S", raising=False)
    monkeypatch.delenv("MODEL_CHECK_CHALLENGER_TIMEOUT_S", raising=False)
    fakes = {"champion": FakeLane("champion"), "challenger": FakeLane("challenger", pid=200)}
    monkeypatch.setattr(model_check, "lane_for", lambda name: fakes[name if name in fakes else "champion"])
    model_check.clear_registry_cache()
    model_check._warmed.clear()
    yield fakes
    model_check.clear_registry_cache()


def request(slots):
    return {"key": {"source_site_id": "s", "site_listing_ref": "r"}, "raw": {}, "snapshot_uri": None, "slots": slots}


def test_each_slot_goes_to_its_own_lane_and_the_results_are_merged(lanes):
    out = model_check.run_scorer(request({"champion": "1", "challenger": "2"}))
    assert [c[1]["request"]["slots"] for c in lanes["champion"].calls] == [{"champion": "1"}]
    assert [c[1]["request"]["slots"] for c in lanes["challenger"].calls] == [{"challenger": "2"}]
    assert out["slots"]["champion"]["pred_price_jmd"] == 1e6 and out["slots"]["challenger"]["pred_price_jmd"] == 2e6
    assert out["input_source"] == "raw_fallback" and out["inputs"]["bedrooms"] == 3.0
    assert {c[0] for lane in lanes.values() for c in lane.calls} == {"score"}


def test_a_null_slot_is_not_sent_anywhere(lanes):
    out = model_check.run_scorer(request({"champion": None, "challenger": "2"}))
    assert lanes["champion"].calls == [] and set(out["slots"]) == {"challenger"}


def test_with_no_slots_the_inputs_are_still_built_on_the_champion_lane(lanes):
    out = model_check.run_scorer(request({"champion": None, "challenger": None}))
    assert len(lanes["champion"].calls) == 1 and lanes["champion"].calls[0][1]["request"]["slots"] == {}
    assert out["inputs"] and out["slots"] == {}


def test_one_failing_lane_only_fails_its_own_slot(lanes):
    lanes["challenger"].outcome = subprocess.TimeoutExpired(cmd="challenger", timeout=900)
    out = model_check.run_scorer(request({"champion": "1", "challenger": "2"}))
    assert out["slots"]["champion"]["pred_price_jmd"] == 1e6
    assert "within 900 s" in out["slots"]["challenger"]["error"]


def test_when_every_lane_fails_the_error_is_raised(lanes):
    lanes["champion"].outcome = ScorerError("The scorer exited with code 3")
    lanes["challenger"].outcome = ScorerError("The scorer exited with code 3")
    with pytest.raises(ScorerError, match="exited with code 3"):
        model_check.run_scorer(request({"champion": "1", "challenger": "2"}))


def test_a_refused_request_says_the_input_row_could_not_be_built(lanes):
    lanes["champion"].outcome = ScorerRequestFailed("KeyError: 'bedrooms'")
    with pytest.raises(ScorerError, match="could not be turned into a model input row: KeyError"):
        model_check.run_scorer(request({"champion": "1", "challenger": None}))


def test_the_challenger_lane_gets_the_longer_timeout(lanes, monkeypatch):
    model_check.run_scorer(request({"champion": "1", "challenger": "2"}))
    assert lanes["champion"].calls[0][2] == 180 and lanes["challenger"].calls[0][2] == 900
    monkeypatch.setenv("MODEL_CHECK_TIMEOUT_S", "1200")  # a larger general timeout wins
    model_check.run_scorer(request({"champion": "1", "challenger": "2"}))
    assert lanes["champion"].calls[1][2] == 1200 and lanes["challenger"].calls[1][2] == 1200
    monkeypatch.setenv("MODEL_CHECK_CHALLENGER_TIMEOUT_S", "3000")
    model_check.run_scorer(request({"champion": None, "challenger": "2"}))
    assert lanes["challenger"].calls[2][2] == 3000


def test_the_lane_spec_runs_serve_py_in_the_monorepo_env(lanes, monkeypatch, tmp_path):
    monkeypatch.setenv("MODEL_CHECK_EXTRA", "tabpfn")
    spec = model_check._lane_spec()
    assert spec.cmd[:3] == ["uv", "run", "--frozen"]
    assert spec.cmd[spec.cmd.index("--project") + 1] == str(tmp_path)
    assert spec.cmd[spec.cmd.index("--extra") + 1] == "tabpfn"
    assert spec.cmd[-1].replace("\\", "/").endswith("backend/scorer/serve.py")
    assert spec.env["MLFLOW_TRACKING_URI"] == "http://reg:5000"
    monkeypatch.setenv("MODEL_CHECK_EXTRA", "prototype")
    assert model_check._lane_spec().signature != spec.signature  # a different extra restarts the lane


def test_lane_for_uses_one_lane_per_slot_name(monkeypatch, tmp_path):
    from app import scorer_client

    (tmp_path / "packages" / "avm_model").mkdir(parents=True)
    monkeypatch.setenv("MODEL_CHECK_ATS_AVM_ROOT", str(tmp_path))
    monkeypatch.setattr(scorer_client, "_lanes", {})
    champion, challenger = model_check.lane_for("champion"), model_check.lane_for("challenger")
    assert champion is not challenger and model_check.lane_for("champion") is champion
    assert model_check.lane_for("something-else") is champion


# ---------- registry cache ----------


def test_registry_reads_are_cached_for_a_minute(monkeypatch):
    model_check.clear_registry_cache()
    calls = []
    clock = {"t": 1000.0}
    monkeypatch.setattr(model_check, "fetch_registry", lambda: calls.append(1) or (VERSIONS, ALIASES))
    monkeypatch.setattr(model_check.time, "monotonic", lambda: clock["t"])
    assert model_check.registry() == (VERSIONS, ALIASES) and model_check.registry() == (VERSIONS, ALIASES)
    assert len(calls) == 1
    clock["t"] += model_check.REGISTRY_TTL_S + 1
    model_check.registry()
    assert len(calls) == 2
    model_check.clear_registry_cache()


def test_a_registry_failure_is_not_cached_and_a_new_uri_is_not_served_stale(monkeypatch):
    model_check.clear_registry_cache()
    state = {"fail": True, "calls": 0}

    def fetch():
        state["calls"] += 1
        if state["fail"]:
            raise RuntimeError("down")
        return VERSIONS, ALIASES

    monkeypatch.setattr(model_check, "fetch_registry", fetch)
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://a:5000")
    with pytest.raises(RuntimeError):
        model_check.registry()
    state["fail"] = False
    assert model_check.registry() == (VERSIONS, ALIASES) and state["calls"] == 2
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://b:5000")
    model_check.registry()
    assert state["calls"] == 3
    model_check.clear_registry_cache()


# ---------- warm-up and the endpoint ----------


@pytest.fixture
def client(monkeypatch, lanes):
    monkeypatch.setenv("LISTINGS_NDJSON_PATH", str(FIXTURE))
    listings.clear_cache()
    monkeypatch.setattr(model_check, "fetch_registry", lambda: (VERSIONS, ALIASES))
    app = FastAPI()
    app.include_router(model_check.router)
    app.dependency_overrides[get_current_user] = lambda: object()
    yield TestClient(app)
    listings.clear_cache()


def test_warm_loads_the_champion_in_the_background_and_not_the_challenger_unless_asked(client, lanes):
    resp = client.post("/api/v1/model-check/warm", json={})
    assert resp.status_code == 202 and resp.json() == {"started": True}
    assert lanes["champion"].calls == [("warm", {"models": ["1"]}, 180)] and lanes["challenger"].calls == []
    client.post("/api/v1/model-check/warm", json={"challenger": "challenger"})
    assert lanes["champion"].calls == [("warm", {"models": ["1"]}, 180)]  # already warm in this process
    assert lanes["challenger"].calls == [("warm", {"models": ["2"]}, 900)]


def test_warm_again_after_the_lane_restarted_or_the_alias_moved(client, lanes):
    client.post("/api/v1/model-check/warm", json={})
    lanes["champion"].pid = 101  # restarted
    client.post("/api/v1/model-check/warm", json={})
    assert len(lanes["champion"].calls) == 2


def test_warm_failures_are_swallowed(client, lanes):
    lanes["champion"].outcome = ScorerError("exited")
    assert client.post("/api/v1/model-check/warm", json={}).status_code == 202


def test_warm_without_a_registry_does_nothing(client, lanes, monkeypatch):
    monkeypatch.setattr(model_check, "fetch_registry", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    model_check.clear_registry_cache()
    assert client.post("/api/v1/model-check/warm", json={}).status_code == 202
    assert lanes["champion"].calls == []
