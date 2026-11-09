"""Batch estimate of the latest run's new listings: selection, the job and its endpoints. Fake lanes, in-memory rows."""
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import listings, model_batch, model_check
from app.auth import get_current_user
from app.scorer_client import ScorerCancelled, ScorerError

VERSIONS = [{"version": "2", "aliases": ["challenger"], "created_at": ""},
            {"version": "1", "aliases": ["champion"], "created_at": ""}]
ALIASES = {"champion": "1", "challenger": "2"}


def obs(ref, run, fetched, ptype="House", ltype="For Sale", amount=10_000_000, currency="JMD", parish="St. Andrew",
        key=True):
    return {
        "record": {"record_id": f"rec-{ref}-{run}"},
        "provenance": {"scrape_run_id": run, "site_listing_ref": ref if key else None, "canonical_url": None,
                       "source_site_id": "cbjamaica.com", "fetched_at": fetched,
                       "snapshot_uri": f"gs://b/{run}/{ref}.html.gz"},
        "listing": {"title": f"Title {ref}", "listing_type_raw": ltype},
        "attributes": {"property_type_raw": ptype, "bedrooms": 3},
        "price": {"amount": amount, "currency": currency},
        "location": {"parish_raw": parish},
    }


OLD, NEW = "run_old", "run_new"


def dataset(extra=()):
    rows = [obs("A", OLD, "2026-11-01T05:00:00"), obs("B", OLD, "2026-11-01T05:01:00"),
            obs("B", NEW, "2026-11-08T05:00:00"),  # seen before: not new
            obs("C", NEW, "2026-11-08T05:01:00", ptype="Apartment", amount=5_000_000),
            obs("D", NEW, "2026-11-08T05:02:00", ptype="Townhouse", amount=20_000_000, parish="St. Catherine"),
            obs("E", NEW, "2026-11-08T05:03:00", ptype="Residential Lot"),  # unsupported type
            obs("F", NEW, "2026-11-08T05:04:00", ltype="For Rent")]  # unsupported listing type
    return rows + list(extra)


# ---------- listings.latest_run_new_listings ----------


def test_new_listings_are_those_in_the_latest_run_and_no_other_run():
    sel = listings.latest_run_new_listings(dataset())
    assert sel["run_id"] == NEW and sel["earlier_runs"] == 1
    assert [listings._g(r, "provenance", "site_listing_ref") for r in sel["new"]] == ["C", "D", "E", "F"]
    assert sel["listings_in_run"] == 5 and sel["unkeyed_rows"] == 0


def test_the_latest_run_is_the_newest_first_fetch_not_the_last_name():
    rows = [obs("A", "z_run_first", "2026-11-01T05:00:00"), obs("B", "a_run_second", "2026-11-08T05:00:00")]
    sel = listings.latest_run_new_listings(rows)
    assert sel["run_id"] == "a_run_second" and [listings._g(r, "provenance", "site_listing_ref") for r in sel["new"]] == ["B"]


def test_a_listing_twice_in_the_latest_run_counts_once_with_its_latest_observation():
    rows = [obs("A", OLD, "2026-11-01T05:00:00"), obs("X", NEW, "2026-11-08T05:00:00", amount=1),
            obs("X", NEW, "2026-11-08T06:00:00", amount=2)]
    sel = listings.latest_run_new_listings(rows)
    assert len(sel["new"]) == 1 and sel["new"][0]["price"]["amount"] == 2


def test_rows_without_a_key_are_counted_not_listed():
    rows = [obs("A", OLD, "2026-11-01T05:00:00"), obs("?", NEW, "2026-11-08T05:00:00", key=False)]
    sel = listings.latest_run_new_listings(rows)
    assert sel["new"] == [] and sel["unkeyed_rows"] == 1


def test_no_runs_means_no_selection():
    assert listings.latest_run_new_listings([]) is None


# ---------- the job ----------


class FakeLane:
    def __init__(self, name, price, fail=None, bad_refs=()):
        self.name, self.price, self.fail, self.bad_refs = name, price, fail, set(bad_refs)
        self.calls = []
        self.block = None  # an Event: prepare waits on it
        self.cancelled = False

    def cancel(self):
        self.cancelled = True
        if self.block is not None:
            self.block.set()

    def call(self, op, payload=None, *, timeout, on_event=None):
        self.calls.append((op, payload, timeout))
        if op == "prepare":
            if self.block is not None:
                self.block.wait(10)
                if self.cancelled:
                    raise ScorerCancelled("cancelled")
            items = payload["items"]
            if on_event:
                on_event({"stage": "pages", "done": len(items), "total": len(items)})
                on_event({"stage": "inputs", "done": len(items), "total": len(items)})
            rows = []
            for it in items:
                ref = it["key"]["site_listing_ref"]
                bad = ref in self.bad_refs
                clean = {"fx_rate_used": 150.0, "price_currency_original": "USD"} if ref == "USDCLEAN" else None
                fx = {"rate": 160.0, "date": "2026-11-06"} if ref == "USDBOJ" else None
                rows.append({"id": it["id"], "key": it["key"], "input_source": "listing_clean" if clean else "raw_fallback",
                             "clean": clean, "fx": fx, "page_error": None, "error": "boom" if bad else None,
                             "inputs": None if bad else {"bedrooms": 3.0, "bathrooms": 2.0, "parish": it["raw"]["parish_raw"],
                                                         "latitude": 18.0, "longitude": -77.0}})
            return {"ok": True, "rows": rows}
        if op == "predict":
            if self.fail is not None:
                raise self.fail
            p = self.price
            return {"ok": True, "results": {r["id"]: {"label": f"m/{payload['model']}", "pred_price_jmd": p,
                                                    "lower_90_jmd": p * 0.8, "upper_90_jmd": p * 1.2,
                                                    "floor_area_flagged": False} for r in payload["rows"]}}
        raise AssertionError(op)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(model_batch, "_current", None)
    model_check.clear_registry_cache()
    state = {"rows": dataset(), "registry": (VERSIONS, ALIASES),
             "lanes": {"champion": FakeLane("champion", 12_000_000.0), "challenger": FakeLane("challenger", 8_000_000.0)}}
    monkeypatch.setattr(listings, "_rows", lambda: state["rows"])
    monkeypatch.setattr(model_check, "lane_for", lambda name: state["lanes"][name if name in state["lanes"] else "champion"])

    def registry():
        reg = state["registry"]
        if isinstance(reg, Exception):
            raise reg
        return reg

    monkeypatch.setattr(model_check, "fetch_registry", registry)
    app = FastAPI()
    app.include_router(model_batch.router)
    app.dependency_overrides[get_current_user] = lambda: object()
    state["client"] = TestClient(app)
    yield state
    model_batch.wait_for_job(10)
    model_check.clear_registry_cache()


def start(env, **body):
    return env["client"].post("/api/v1/model-check/batch", json=body)


def finished(env, **body):
    resp = start(env, **body)
    assert resp.status_code == 200, resp.text
    model_batch.wait_for_job(20)
    out = env["client"].get("/api/v1/model-check/batch").json()
    assert out["status"] != "running"
    return out


def rows_by_ref(out):
    return {r["ref"]: r for r in out["rows"]}


def test_requires_auth():
    app = FastAPI()
    app.include_router(model_batch.router)

    def deny():
        from fastapi import HTTPException

        raise HTTPException(401, "Not authenticated")

    app.dependency_overrides[get_current_user] = deny
    assert TestClient(app).get("/api/v1/model-check/batch").status_code == 401


def test_nothing_before_a_batch_has_started(env):
    assert env["client"].get("/api/v1/model-check/batch").json() == {"job": None}


def test_scores_the_new_supported_listings_and_lists_the_others_with_reasons(env):
    out = finished(env)
    assert out["status"] == "done" and out["run"]["run_id"] == NEW
    assert set(rows_by_ref(out)) == {"C", "D"}  # B is not new; E and F are not covered by the model
    reasons = {u["ref"]: u["reason"] for u in out["unscored"]}
    assert set(reasons) == {"E", "F"} and all("for-sale houses, apartments and townhouses" in r for r in reasons.values())
    assert out["run"]["new_listings"] == 4 and out["run"]["supported"] == 2 and out["counts"] == {"scoring": 2, "unscored": 2}
    c = rows_by_ref(out)["C"]
    assert c["slots"]["champion"]["status"] == "ok" and c["slots"]["champion"]["pred_price_jmd"] == 12_000_000
    assert round(c["slots"]["champion"]["gap_pct"]) == 140 and round(c["slots"]["challenger"]["gap_pct"]) == 60
    assert c["asking"] == {"amount": 5_000_000, "currency": "JMD", "jmd": 5_000_000}
    assert c["property_type"] == "Apartment" and c["parish_input"] == "St. Andrew" and c["bedrooms"] == 3.0


def test_each_slot_is_predicted_once_for_all_rows_and_inputs_are_prepared_once(env):
    finished(env)
    lanes = env["lanes"]
    prepares = [c for c in lanes["champion"].calls if c[0] == "prepare"]
    assert len(prepares) == 1 and [i["id"] for i in prepares[0][1]["items"]] == ["rec-C-run_new", "rec-D-run_new"]
    assert prepares[0][1]["items"][0]["key"] == {"source_site_id": "cbjamaica.com", "site_listing_ref": "C"}
    assert prepares[0][1]["items"][0]["snapshot_uri"] == "gs://b/run_new/C.html.gz"
    assert [c[0] for c in lanes["champion"].calls] == ["prepare", "predict"]
    assert [c[0] for c in lanes["challenger"].calls] == ["predict"]
    predict = lanes["challenger"].calls[0]
    assert predict[1]["model"] == "2" and len(predict[1]["rows"]) == 2 and set(predict[1]["rows"][0]) == {"id", "inputs"}
    assert predict[2] == 3600  # the batch timeout, not the per-click one


def test_large_gaps_are_flagged_and_the_threshold_is_reported(env):
    out = finished(env)
    assert out["gap_threshold_pct"] == model_batch.GAP_FLAG_PCT == 25.0
    flagged = {r["ref"]: r["large_gap"] for r in out["rows"]}
    assert flagged == {"C": True, "D": True}  # D: asking 20M vs champion 12M (-40%)


def test_summary_gives_the_median_gap_by_property_type_and_parish(env):
    out = finished(env)
    by_type = {e["value"]: e for e in out["summary"]["by_property_type"]}
    assert set(by_type) == {"Apartment", "Townhouse"}
    assert round(by_type["Apartment"]["champion_median_gap_pct"]) == 140 and by_type["Townhouse"]["champion_gap_count"] == 1
    by_parish = {e["value"]: e for e in out["summary"]["by_parish"]}
    assert set(by_parish) == {"St. Andrew", "St. Catherine"}
    assert out["summary"]["overall"]["count"] == 2 and out["summary"]["overall"]["large_gap_count"] == 2


def test_polling_with_the_current_rows_version_leaves_out_the_rows(env):
    out = finished(env)
    again = env["client"].get("/api/v1/model-check/batch", params={"since": out["rows_version"]}).json()
    assert "rows" not in again and again["status"] == "done" and again["rows_version"] == out["rows_version"]
    stale = env["client"].get("/api/v1/model-check/batch", params={"since": out["rows_version"] - 1}).json()
    assert len(stale["rows"]) == 2


def test_progress_and_slot_timings_are_reported(env):
    out = finished(env)
    assert out["progress"]["prepare"] == {"stage": "inputs", "done": 2, "total": 2}
    assert out["slots"]["champion"]["state"] == "done" and out["slots"]["challenger"]["state"] == "done"
    assert out["slots"]["challenger"]["version"] == "2"
    assert out["slots"]["challenger"]["finished_at"] >= out["slots"]["challenger"]["started_at"]


def test_a_failing_challenger_lane_leaves_the_champion_results(env):
    env["lanes"]["challenger"].fail = ScorerError("The scorer exited with code 3")
    out = finished(env)
    assert out["status"] == "done"
    assert out["slots"]["challenger"]["state"] == "error" and "exited with code 3" in out["slots"]["challenger"]["message"]
    c = rows_by_ref(out)["C"]
    assert c["slots"]["champion"]["status"] == "ok"
    assert c["slots"]["challenger"] == {"status": "error", "message": "The scorer exited with code 3"}


def test_rows_the_scorer_cannot_prepare_are_listed_as_unscored(env):
    env["lanes"]["champion"].bad_refs = {"D"}
    out = finished(env)
    assert set(rows_by_ref(out)) == {"C"}
    d = next(u for u in out["unscored"] if u["ref"] == "D")
    assert "could not be built" in d["reason"] and "boom" in d["reason"]
    assert [c for c in env["lanes"]["challenger"].calls][0][1]["rows"][0]["id"] == "rec-C-run_new"


def test_the_challenger_can_be_left_out(env):
    out = finished(env, challenger=None)
    assert env["lanes"]["challenger"].calls == []
    assert out["slots"]["challenger"]["state"] == "skipped"
    assert rows_by_ref(out)["C"]["slots"]["challenger"] == {"status": "skipped"}


def test_an_unregistered_challenger_alias_is_reported(env):
    env["registry"] = (VERSIONS[1:], {"champion": "1"})
    out = finished(env)
    assert out["slots"]["challenger"]["state"] == "not_registered"
    assert rows_by_ref(out)["C"]["slots"]["challenger"] == {"status": "not_registered"}
    assert rows_by_ref(out)["C"]["slots"]["champion"]["status"] == "ok"


def test_nothing_to_score_with_is_an_error(env):
    env["registry"] = ([], {})
    out = finished(env)
    assert out["status"] == "error" and "No model to score with" in out["message"]


def test_registry_down_is_the_jobs_error(env):
    env["registry"] = RuntimeError("connection refused")
    out = finished(env)
    assert out["status"] == "error" and "gcloud run services proxy" in out["message"]


def test_a_limit_scores_only_the_first_listings(env):
    out = finished(env, limit=1)
    assert [r["ref"] for r in out["rows"]] == ["C"] and out["run"]["limit"] == 1 and out["run"]["supported"] == 2


def test_usd_asking_is_converted_with_the_clean_tables_rate_only(env):
    env["rows"] = dataset([obs("USDCLEAN", NEW, "2026-11-08T05:05:00", amount=100_000, currency="USD"),
                           obs("USDRAW", NEW, "2026-11-08T05:06:00", amount=100_000, currency="USD")])
    out = finished(env)
    by = rows_by_ref(out)
    assert by["USDCLEAN"]["asking"]["jmd"] == 15_000_000.0 and round(by["USDCLEAN"]["slots"]["champion"]["gap_pct"]) == -20
    assert by["USDRAW"]["asking"]["jmd"] is None and by["USDRAW"]["slots"]["champion"]["gap_pct"] is None
    assert by["USDRAW"]["slots"]["champion"]["status"] == "ok"  # still scored, just no gap


def test_a_usd_listing_outside_the_clean_table_uses_the_rate_the_scorer_looked_up(env):
    env["rows"] = dataset([obs("USDBOJ", NEW, "2026-11-08T05:05:00", amount=100_000, currency="USD")])
    by = rows_by_ref(finished(env))
    row = by["USDBOJ"]
    assert row["asking"]["jmd"] == 16_000_000.0
    assert row["asking"]["fx"] == {"rate": 160.0, "date": "2026-11-06"}
    assert round(row["slots"]["champion"]["gap_pct"]) == -25  # 12M against 16M
    assert "fx" not in by["C"]["asking"]  # JMD listings need no rate


def test_a_second_start_while_running_is_refused_and_cancel_stops_the_run(env):
    release = threading.Event()
    env["lanes"]["champion"].block = release
    assert start(env).status_code == 200
    assert start(env).status_code == 409
    cancelled = env["client"].post("/api/v1/model-check/batch/cancel").json()
    assert cancelled == {"cancelled": True}
    model_batch.wait_for_job(20)
    out = env["client"].get("/api/v1/model-check/batch").json()
    assert out["status"] == "cancelled" and "cancelled" in out["message"]
    assert env["lanes"]["champion"].cancelled and env["lanes"]["challenger"].cancelled
    assert start(env).status_code == 200  # a new batch can start afterwards


def test_cancel_without_a_running_batch_does_nothing(env):
    assert env["client"].post("/api/v1/model-check/batch/cancel").json() == {"cancelled": False}
    finished(env)
    assert env["client"].post("/api/v1/model-check/batch/cancel").json() == {"cancelled": False}


def test_no_scrape_runs_is_a_404(env):
    env["rows"] = []
    assert start(env).status_code == 404


# ---------- pure helpers ----------


def test_summarize_ignores_rows_without_a_gap_and_orders_groups_by_size():
    def row(ptype, parish, champion_gap):
        slot = {"status": "ok", "gap_pct": champion_gap} if champion_gap is not None else {"status": "ok", "gap_pct": None}
        return {"property_type": ptype, "parish": parish, "slots": {"champion": slot, "challenger": {"status": "skipped"}}}

    summary = model_batch.summarize([row("House", "A", 10.0), row("House", "A", 30.0), row("House", "B", None),
                                     row("Apartment", None, -50.0)])
    assert [e["value"] for e in summary["by_property_type"]] == ["House", "Apartment"]
    house = summary["by_property_type"][0]
    assert house["count"] == 3 and house["champion_median_gap_pct"] == 20.0 and house["champion_gap_count"] == 2
    assert house["challenger_median_gap_pct"] is None
    assert "(unknown)" in {e["value"] for e in summary["by_parish"]}
    assert summary["overall"]["large_gap_count"] == 2  # 30 and -50
