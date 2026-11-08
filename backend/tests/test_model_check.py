"""Model-estimate router tests: a mini app mounts only the model-check router against a 3-row NDJSON fixture.
The registry fetch and the scorer runner are faked; no network and no subprocess."""
import json
import os
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import listings, model_check
from app.auth import get_current_user

FIXTURE = Path(__file__).parent / "fixtures" / "model_check_sample.ndjson"

VERSIONS = [
    {"version": "2", "aliases": ["challenger"], "created_at": "2026-11-05T10:00:00+00:00"},
    {"version": "1", "aliases": ["champion"], "created_at": "2026-11-01T10:00:00+00:00"},
]
ALIASES = {"champion": "1", "challenger": "2"}


def slot_result(price, label):
    return {
        "label": label,
        "pred_price_jmd": price,
        "lower_80_jmd": price * 0.8,
        "upper_80_jmd": price * 1.2,
        "lower_90_jmd": price * 0.7,
        "upper_90_jmd": price * 1.3,
        "lower_95_jmd": price * 0.6,
        "upper_95_jmd": price * 1.4,
        "floor_area_flagged": False,
    }


def scorer_response(**over):
    resp = {
        "input_source": "listing_clean",
        "clean": {
            "record_id": "rec_apt",
            "fetched_at": "2026-10-13 10:00:00",
            "trainable": True,
            "fx_rate_used": 1.0,
            "price_currency_original": "JMD",
            "price_jmd": 850_000_000.0,
            "blocking_flags": [],
        },
        "inputs": {"bedrooms": 3.0, "bathrooms": 2.0, "latitude": 18.0, "longitude": -77.0},
        "slots": {
            "champion": slot_result(612e6, "avm_listing_price/1"),
            "challenger": slot_result(700e6, "avm_listing_price/2"),
        },
        "page_error": None,
    }
    resp.update(over)
    return resp


class FakeRunner:
    def __init__(self, response=None, exc=None):
        self.response = response if response is not None else scorer_response()
        self.exc = exc
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if self.exc:
            raise self.exc
        return self.response


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("LISTINGS_NDJSON_PATH", str(FIXTURE))
    listings.clear_cache()
    app = FastAPI()
    app.include_router(model_check.router)
    app.dependency_overrides[get_current_user] = lambda: object()
    state = {"runner": FakeRunner(), "registry": (VERSIONS, ALIASES)}

    def runner(request):
        return state["runner"](request)

    def registry():
        reg = state["registry"]
        if isinstance(reg, Exception):
            raise reg
        return reg

    monkeypatch.setattr(model_check, "run_scorer", runner)
    monkeypatch.setattr(model_check, "fetch_registry", registry)
    state["client"] = TestClient(app)
    yield state
    listings.clear_cache()


def score(env, record_id, **body):
    return env["client"].post("/api/v1/model-check/score", json={"record_id": record_id, **body})


def test_requires_auth(monkeypatch):
    monkeypatch.setenv("LISTINGS_NDJSON_PATH", str(FIXTURE))
    app = FastAPI()
    app.include_router(model_check.router)

    def deny():
        raise HTTPException(401, "Not authenticated")

    app.dependency_overrides[get_current_user] = deny
    assert TestClient(app).get("/api/v1/model-check/versions").status_code == 401


def test_is_supported():
    # Same exact match as training: whitespace is trimmed and collapsed, case is not folded.
    for ptype in ("House", "Apartment", "Townhouse", " Apartment ", "House\n"):
        assert model_check.is_supported({"listing_type_raw": "For Sale", "property_type_raw": ptype}) == (True, None)
    assert model_check.is_supported({"listing_type_raw": " For  Sale ", "property_type_raw": "House"}) == (True, None)
    for ptype in ("house", "APARTMENT", None):
        assert not model_check.is_supported({"listing_type_raw": "For Sale", "property_type_raw": ptype})[0]
    assert not model_check.is_supported({"listing_type_raw": "for sale", "property_type_raw": "House"})[0]
    ok, reason = model_check.is_supported({"listing_type_raw": "For Sale", "property_type_raw": "Land"})
    assert not ok and "for-sale houses, apartments and townhouses" in reason
    ok, reason = model_check.is_supported({"listing_type_raw": "For Rent", "property_type_raw": "House"})
    assert not ok and reason


def test_asking_jmd_and_gap():
    assert model_check.asking_jmd(1_000_000, "USD", 160.0) == 160_000_000.0
    assert model_check.asking_jmd(5.0, "JMD", None) == 5.0
    assert model_check.asking_jmd(1.0, "USD", None) is None
    assert model_check.asking_jmd(None, "JMD", None) is None
    assert round(model_check.gap_pct(612e6, 850e6), 1) == -28.0
    assert model_check.gap_pct(1.0, None) is None
    assert model_check.gap_pct(1.0, 0) is None


def test_unsupported_listing_does_not_start_scorer(env):
    body = score(env, "rec_land").json()
    assert body["supported"] is False
    assert "for-sale houses, apartments and townhouses" in body["unsupported_reason"]
    assert env["runner"].requests == []


def test_scores_apartment_with_warnings(env):
    env["runner"] = FakeRunner(
        scorer_response(
            clean={
                "record_id": "rec_apt",
                "trainable": False,
                "fx_rate_used": 1.0,
                "blocking_flags": ["bedrooms_implausible"],
            },
            inputs={"bedrooms": 45.0, "bathrooms": 60.0, "latitude": None, "longitude": None},
        )
    )
    resp = score(env, "rec_apt")
    assert resp.status_code == 200
    body = resp.json()
    assert body["supported"] is True and body["unsupported_reason"] is None
    flags = {i["name"]: i["flag"] for i in body["inputs"]}
    assert flags["bedrooms"] == "outlier" and flags["bathrooms"] == "outlier" and flags["latitude"] is None
    assert {i["name"]: i["value"] for i in body["inputs"]}["bedrooms"] == 45.0
    codes = {w["code"] for w in body["warnings"]}
    assert {"input_out_of_range", "blocked_from_training", "geo_missing"} <= codes
    assert "probably_in_training" not in codes and "not_in_clean_table" not in codes
    blocked = next(w for w in body["warnings"] if w["code"] == "blocked_from_training")
    assert "bedrooms_implausible" in blocked["message"]
    assert body["asking"] == {"amount": 850_000_000, "currency": "JMD", "jmd": 850_000_000}
    champ = body["slots"]["champion"]
    assert champ["status"] == "ok" and champ["label"] == "avm_listing_price/1"
    assert round(champ["gap_pct"], 1) == -28.0
    assert champ["floor_area_flagged"] is False
    assert body["registry_error"] is None
    # aliases are resolved to version numbers before the scorer sees them
    req = env["runner"].requests[0]
    assert req["slots"] == {"champion": "1", "challenger": "2"}
    assert req["key"] == {"source_site_id": "cbjamaica.com", "site_listing_ref": "MLS-99213"}
    assert req["snapshot_uri"] == "gs://b/MLS-99213.html.gz"
    assert req["raw"]["bedrooms"] == 45 and req["raw"]["property_type_raw"] == "Apartment"


def test_training_and_provenance_warnings(env):
    codes = {w["code"] for w in score(env, "rec_apt").json()["warnings"]}
    assert "probably_in_training" in codes

    env["runner"] = FakeRunner(scorer_response(input_source="raw_fallback", clean=None))
    codes = {w["code"] for w in score(env, "rec_apt").json()["warnings"]}
    assert "not_in_clean_table" in codes and "probably_in_training" not in codes

    clean = scorer_response()["clean"] | {"record_id": "rec_other"}
    env["runner"] = FakeRunner(scorer_response(clean=clean))
    differs = next(w for w in score(env, "rec_apt").json()["warnings"] if w["code"] == "observation_differs")
    assert "rec_other" in differs["message"] and "rec_apt" in differs["message"]
    assert "page fields from the observation on screen" in differs["message"]


def test_floor_area_and_page_warnings(env):
    resp = scorer_response(page_error="fetch failed")
    resp["slots"]["champion"]["floor_area_flagged"] = True
    env["runner"] = FakeRunner(resp)
    codes = {w["code"] for w in score(env, "rec_apt").json()["warnings"]}
    assert {"floor_area_ignored", "page_fields_unavailable"} <= codes


def test_usd_listing_converted_with_clean_rate(env):
    clean = scorer_response()["clean"] | {"record_id": "rec_house", "fx_rate_used": 160.0,
                                          "price_currency_original": "USD"}
    env["runner"] = FakeRunner(scorer_response(clean=clean))
    body = score(env, "rec_house").json()
    assert "currency_differs" not in {w["code"] for w in body["warnings"]}
    assert body["asking"]["jmd"] == 160_000_000.0
    assert body["asking"]["currency"] == "USD" and body["asking"]["amount"] == 1_000_000
    assert round(body["slots"]["champion"]["gap_pct"], 1) == round((612e6 / 160e6 - 1) * 100, 1)


def test_usd_on_screen_but_jmd_in_clean_table_has_no_gap(env):
    """listing_clean sets fx_rate_used = 1.0 for JMD rows; applying it to a USD amount would be wrong ~150x."""
    clean = scorer_response()["clean"] | {"record_id": "rec_house", "fx_rate_used": 1.0,
                                          "price_currency_original": "JMD"}
    env["runner"] = FakeRunner(scorer_response(clean=clean))
    body = score(env, "rec_house").json()
    assert body["asking"]["currency"] == "USD" and body["asking"]["jmd"] is None
    assert body["slots"]["champion"]["status"] == "ok" and body["slots"]["champion"]["gap_pct"] is None
    differs = next(w for w in body["warnings"] if w["code"] == "currency_differs")
    assert "priced in USD" in differs["message"] and "JMD" in differs["message"]


def test_jmd_on_screen_is_used_as_is_whatever_the_clean_currency(env):
    clean = scorer_response()["clean"] | {"fx_rate_used": 160.0, "price_currency_original": "USD"}
    env["runner"] = FakeRunner(scorer_response(clean=clean))
    body = score(env, "rec_apt").json()
    assert body["asking"]["jmd"] == 850_000_000
    assert round(body["slots"]["champion"]["gap_pct"], 1) == -28.0
    assert "currency_differs" not in {w["code"] for w in body["warnings"]}


def test_not_in_clean_table_message_mentions_missing_jmd_price_for_foreign_currency(env):
    env["runner"] = FakeRunner(scorer_response(input_source="raw_fallback", clean=None))
    usd = next(w for w in score(env, "rec_house").json()["warnings"] if w["code"] == "not_in_clean_table")
    assert "no JMD asking price or gap" in usd["message"]
    jmd = next(w for w in score(env, "rec_apt").json()["warnings"] if w["code"] == "not_in_clean_table")
    assert "no JMD asking price" not in jmd["message"]


def test_location_features_missing_warning(env):
    # the default inputs have coordinates but none of the location features
    codes = {w["code"] for w in score(env, "rec_apt").json()["warnings"]}
    assert "location_features_missing" in codes and "geo_missing" not in codes
    warning = next(w for w in score(env, "rec_apt").json()["warnings"] if w["code"] == "location_features_missing")
    assert "not computed" in warning["message"]

    with_features = scorer_response(inputs={"latitude": 18.0, "longitude": -77.0, "dist_coast_m": 1200.0})
    env["runner"] = FakeRunner(with_features)
    assert "location_features_missing" not in {w["code"] for w in score(env, "rec_apt").json()["warnings"]}

    no_coords = scorer_response(inputs={"latitude": None, "longitude": None})
    env["runner"] = FakeRunner(no_coords)
    codes = {w["code"] for w in score(env, "rec_apt").json()["warnings"]}
    assert "geo_missing" in codes and "location_features_missing" not in codes


def test_scored_at_is_a_utc_timestamp(env):
    import datetime as dt

    for record_id in ("rec_apt", "rec_land"):  # scored and unsupported
        stamp = dt.datetime.fromisoformat(score(env, record_id).json()["scored_at"])
        assert stamp.tzinfo is not None and stamp.utcoffset() == dt.timedelta(0)
        assert abs((dt.datetime.now(dt.timezone.utc) - stamp).total_seconds()) < 60


def test_usd_listing_without_rate_has_no_gap(env):
    env["runner"] = FakeRunner(scorer_response(input_source="raw_fallback", clean=None))
    body = score(env, "rec_house").json()
    assert body["asking"]["jmd"] is None
    assert body["slots"]["champion"]["status"] == "ok" and body["slots"]["champion"]["gap_pct"] is None


def test_no_price_still_scores(env, tmp_path, monkeypatch):
    rec = json.loads(FIXTURE.read_text(encoding="utf-8").splitlines()[2])
    rec["record"]["record_id"] = "rec_noprice"
    rec["price"]["amount"] = None
    path = tmp_path / "noprice.ndjson"
    path.write_text(json.dumps(rec), encoding="utf-8")
    monkeypatch.setenv("LISTINGS_NDJSON_PATH", str(path))
    listings.clear_cache()
    body = score(env, "rec_noprice").json()
    assert body["asking"]["jmd"] is None
    assert body["slots"]["champion"]["status"] == "ok"
    assert body["slots"]["champion"]["gap_pct"] is None


def test_challenger_alias_missing(env):
    env["registry"] = (VERSIONS[1:], {"champion": "1"})
    body = score(env, "rec_apt").json()
    assert body["slots"]["challenger"] == {"status": "not_registered"}
    assert body["slots"]["champion"]["status"] == "ok"
    assert env["runner"].requests[0]["slots"] == {"champion": "1", "challenger": None}


def test_version_number_accepted_and_challenger_can_be_off(env):
    body = score(env, "rec_apt", champion="2", challenger=None).json()
    assert env["runner"].requests[0]["slots"] == {"champion": "2", "challenger": None}
    assert body["slots"]["challenger"] == {"status": "skipped"}
    assert score(env, "rec_apt", champion="9").json()["slots"]["champion"] == {"status": "not_registered"}


def test_scorer_slot_error_is_reported(env):
    resp = scorer_response()
    resp["slots"]["challenger"] = {"error": "boom"}
    env["runner"] = FakeRunner(resp)
    slots = score(env, "rec_apt").json()["slots"]
    assert slots["challenger"] == {"status": "error", "message": "boom"}
    assert slots["champion"]["status"] == "ok"


def test_registry_down_still_returns_inputs(env):
    env["registry"] = RuntimeError("connection refused")
    body = score(env, "rec_apt").json()
    assert "gcloud run services proxy" in body["registry_error"]
    assert env["runner"].requests[0]["slots"] == {"champion": None, "challenger": None}
    assert body["inputs"]
    assert body["slots"]["champion"]["status"] == "error"


@pytest.mark.parametrize(
    "exc",
    [subprocess.TimeoutExpired(cmd="uv", timeout=180), FileNotFoundError("uv"), model_check.ScorerError("bad output")],
)
def test_scorer_failures_never_500(env, exc):
    env["runner"] = FakeRunner(exc=exc)
    resp = score(env, "rec_apt")
    assert resp.status_code == 200
    body = resp.json()
    for name in ("champion", "challenger"):
        assert body["slots"][name]["status"] == "error" and body["slots"][name]["message"]
    assert body["inputs"] == []


def test_unknown_record_404(env):
    assert score(env, "nope").status_code == 404


def test_versions_endpoint(env):
    body = env["client"].get("/api/v1/model-check/versions").json()
    assert body == {"model": "avm_listing_price", "versions": VERSIONS, "aliases": ALIASES}


def test_versions_endpoint_unreachable_is_503(env):
    env["registry"] = RuntimeError("down")
    resp = env["client"].get("/api/v1/model-check/versions")
    assert resp.status_code == 503
    assert "gcloud run services proxy mlflow --project ats-avm-jam --region us-central1 --port 5000" in resp.json()["detail"]


def test_versions_endpoint_timeout_is_503_with_cold_start_hint(env):
    env["registry"] = model_check.httpx.ReadTimeout("timed out")
    resp = env["client"].get("/api/v1/model-check/versions")
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert "cold-starting" in detail and "unreachable" not in detail
    assert "gcloud run services proxy mlflow" in detail


# ---- the real registry reader and subprocess runner, with their transports faked ----


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def test_fetch_registry_maps_mlflow_payload(monkeypatch):
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append((url, params, timeout))
        if url.endswith("/model-versions/search"):
            return FakeResponse(
                {
                    "model_versions": [
                        {"name": "avm_listing_price", "version": "1", "creation_timestamp": "1761991200000"},
                        {"name": "avm_listing_price", "version": "10", "creation_timestamp": "1762336800000"},
                        {"name": "avm_listing_price", "version": "2", "creation_timestamp": "1762077600000"},
                    ]
                }
            )
        return FakeResponse(
            {"registered_model": {"aliases": [{"alias": "champion", "version": "1"}, {"alias": "challenger", "version": "10"}]}}
        )

    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://reg:5000/")
    monkeypatch.setattr(model_check.httpx, "get", fake_get)
    versions, aliases = model_check.fetch_registry()
    assert aliases == {"champion": "1", "challenger": "10"}
    assert [v["version"] for v in versions] == ["10", "2", "1"]
    assert versions[0]["aliases"] == ["challenger"] and versions[2]["aliases"] == ["champion"]
    assert versions[0]["created_at"].startswith("2025-11") or versions[0]["created_at"].startswith("2026-")
    assert [c[2] for c in calls] == [model_check.REGISTRY_TIMEOUT_S] * 2 and model_check.REGISTRY_TIMEOUT_S == 90
    assert calls[0][0] == "http://reg:5000/api/2.0/mlflow/model-versions/search"
    assert calls[0][1]["filter"] == "name='avm_listing_price'"
    assert calls[1][0] == "http://reg:5000/api/2.0/mlflow/registered-models/get"
    assert calls[1][1] == {"name": "avm_listing_price"}


REQUEST = {"key": {"source_site_id": "s", "site_listing_ref": "r"}, "raw": {}, "snapshot_uri": None, "slots": {}}


class Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


@pytest.fixture
def real_runner(monkeypatch, tmp_path):
    (tmp_path / "packages" / "avm_model").mkdir(parents=True)
    monkeypatch.setenv("MODEL_CHECK_ATS_AVM_ROOT", str(tmp_path))
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://reg:5000")
    calls = []

    def install(completed):
        def fake_run(cmd, **kw):
            calls.append((cmd, kw))
            if isinstance(completed, Exception):
                raise completed
            return completed

        monkeypatch.setattr(model_check, "_run_child", fake_run)
        return calls

    return install, tmp_path


def test_real_runner_command_and_parse(real_runner):
    install, root = real_runner
    calls = install(Completed(0, 'chatter\n{"inputs": {"a": 1}, "slots": {}}\n'))
    assert model_check.run_scorer(REQUEST) == {"inputs": {"a": 1}, "slots": {}}
    cmd, kw = calls[0]
    assert cmd[:3] == ["uv", "run", "--frozen"] and "--package" in cmd and "avm_model" in cmd and "--extra" in cmd
    assert cmd[cmd.index("--extra") + 1] == "prototype"
    assert cmd[cmd.index("--project") + 1] == str(root)
    assert cmd[-1].replace("\\", "/").endswith("backend/scorer/score_listing.py")
    assert json.loads(kw["input"]) == REQUEST
    assert kw["timeout"] == 180
    assert kw["env"]["MLFLOW_TRACKING_URI"] == "http://reg:5000"


def test_real_runner_drops_interpreter_variables_from_child_env(real_runner, monkeypatch):
    install, _ = real_runner
    for name in ("PYTHONHOME", "PYTHONPATH", "UV_INTERNAL__PYTHONHOME", "VIRTUAL_ENV"):
        monkeypatch.setenv(name, "/some/venv")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("SOME_OTHER_VAR", "kept")
    calls = install(Completed(0, '{"inputs": {}, "slots": {}}'))
    model_check.run_scorer(REQUEST)
    env = calls[0][1]["env"]
    assert not {"PYTHONHOME", "PYTHONPATH", "UV_INTERNAL__PYTHONHOME", "VIRTUAL_ENV"} & set(env)
    assert env["MLFLOW_TRACKING_URI"] == "http://reg:5000"
    assert env["PATH"] == "/usr/bin" and env["SOME_OTHER_VAR"] == "kept"


@pytest.mark.parametrize(
    "completed",
    [
        Completed(1, "", "Traceback...\nValueError: nope"),
        Completed(2, '{"fatal": "KeyError: x"}', ""),
        Completed(0, "", ""),
        Completed(0, "not json", ""),
        Completed(0, "[1, 2]", ""),
    ],
)
def test_real_runner_failures_raise_scorer_error(real_runner, completed):
    install, _ = real_runner
    install(completed)
    with pytest.raises(model_check.ScorerError):
        model_check.run_scorer(REQUEST)


def test_real_runner_failure_message_includes_stderr_tail(real_runner):
    install, _ = real_runner
    install(Completed(1, "", "Traceback...\nValueError: nope"))
    with pytest.raises(model_check.ScorerError, match="ValueError: nope"):
        model_check.run_scorer(REQUEST)


def test_real_runner_nan_becomes_null(real_runner):
    install, _ = real_runner
    install(Completed(0, '{"inputs": {"a": NaN}, "slots": {}}'))
    assert model_check.run_scorer(REQUEST)["inputs"] == {"a": None}


def test_real_runner_missing_monorepo(monkeypatch, tmp_path):
    monkeypatch.setenv("MODEL_CHECK_ATS_AVM_ROOT", str(tmp_path))
    with pytest.raises(model_check.ScorerError, match="MODEL_CHECK_ATS_AVM_ROOT"):
        model_check.run_scorer(REQUEST)


def test_default_root_is_the_enclosing_monorepo(monkeypatch):
    monkeypatch.delenv("MODEL_CHECK_ATS_AVM_ROOT", raising=False)
    assert model_check._ats_avm_root() == Path(model_check.__file__).resolve().parents[4]


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":  # os.kill(pid, 0) would terminate the process on Windows
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out.split()
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _force_kill(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def test_run_child_timeout_kills_grandchildren_and_returns_promptly(monkeypatch, tmp_path):
    """`uv run` starts python as a grandchild that inherits the pipes. The timeout must kill it; if only the direct
    child died, the pipes would stay open and the call would wait out the whole drain period."""
    import sys
    import time

    monkeypatch.setattr(model_check, "KILL_DRAIN_TIMEOUT_S", 6)
    pid_file = tmp_path / "grandchild.pid"
    script = (
        "import subprocess, sys, time\n"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "open(sys.argv[1], 'w').write(str(g.pid))\n"
        "time.sleep(60)\n"
    )
    grandchild_pid = None
    try:
        started = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            model_check._run_child([sys.executable, "-c", script, str(pid_file)], input="", timeout=2,
                                   env=dict(os.environ))
        elapsed = time.monotonic() - started
        assert pid_file.exists(), "test setup: the grandchild never started"
        grandchild_pid = int(pid_file.read_text())
        # Killed tree: about the 2 s timeout. Only the direct child killed: 2 s + the 6 s drain.
        assert elapsed < 5
        for _ in range(30):  # taskkill / SIGKILL take a moment to land
            if not _pid_alive(grandchild_pid):
                break
            time.sleep(0.1)
        assert not _pid_alive(grandchild_pid)
    finally:
        if grandchild_pid is None and pid_file.exists():
            grandchild_pid = int(pid_file.read_text())
        if grandchild_pid is not None and _pid_alive(grandchild_pid):
            _force_kill(grandchild_pid)


@pytest.mark.parametrize("exc", [RuntimeError("worker cancelled"), KeyboardInterrupt()])
def test_run_child_kills_the_tree_on_any_exception_and_reraises_it(monkeypatch, exc):
    events = []

    class FakeProc:
        pid = 4242

        def communicate(self, input=None, timeout=None):
            events.append(("communicate", timeout))
            if len(events) == 1:
                raise exc
            return "", ""

    monkeypatch.setattr(model_check.subprocess, "Popen", lambda *a, **kw: FakeProc())
    monkeypatch.setattr(model_check, "_kill_tree", lambda proc: events.append(("kill", proc.pid)))
    with pytest.raises(type(exc)) as caught:
        model_check._run_child(["uv"], input="{}", timeout=5, env={})
    assert caught.value is exc
    assert [e[0] for e in events] == ["communicate", "kill", "communicate"]  # killed, then reaped
    assert events[1] == ("kill", 4242)


def test_run_child_returns_completed_process():
    import sys

    done = model_check._run_child([sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"],
                                  input="abc", timeout=30, env=dict(os.environ))
    assert done.returncode == 0 and done.stdout.strip() == "ABC"
