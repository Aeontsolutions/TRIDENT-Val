"""The long-lived scorer server: line-delimited JSON requests in, event and response lines out. Fakes only."""
import json

import pandas as pd

from scorer import serve
from scorer.score_listing import Deps

COLUMNS = ["property_type", "bedrooms", "pool"]
KEY = {"source_site_id": "s", "site_listing_ref": "MLS-1"}


class EchoModel:
    def predict(self, frame):
        p = frame["bedrooms"].to_numpy() * 1e6
        return pd.DataFrame({
            "pred_price_jmd": p, "lower_80_jmd": p, "upper_80_jmd": p, "lower_90_jmd": p, "upper_90_jmd": p,
            "lower_95_jmd": p, "upper_95_jmd": p, "floor_area_flagged": [False] * len(p),
            "model_version": ["m/1"] * len(p),
        })


class Counter:
    def __init__(self):
        self.joined = 0
        self.factory = 0
        self.loaded = []
        self.explained = []


def make_factory(counter, fail_first=False, joined_exc=None, bad_ref=None):
    def factory():
        counter.factory += 1
        if fail_first and counter.factory == 1:
            raise ImportError("avm_model missing")

        def fetch_joined(site, ref):
            counter.joined += 1
            if joined_exc:
                raise joined_exc
            return None

        def load_model(ref):
            counter.loaded.append(ref)
            if ref == bad_ref:
                raise KeyError("no such version")
            return EchoModel()

        def explain(ref, inputs, key):
            counter.explained.append((ref, inputs, key))
            return {"drivers": [{"group": "Bedrooms", "pct_effect": 0.1}], "comps": {"available": False}}

        return Deps(columns=lambda: list(COLUMNS), fetch_joined=fetch_joined,
                    fetch_geo_spatial=lambda s, r: (None, None), fetch_page_fields=lambda uri: {},
                    load_model=load_model, categorical=lambda: ["property_type"], explain=explain)

    return factory


def serve_all(requests, factory, **kw):
    out = []
    serve.serve_loop([r if isinstance(r, str) else json.dumps(r) for r in requests], out.append, factory, **kw)
    return out


def final(out, rid):
    return next(m for m in out if m.get("id") == rid and "event" not in m)


def score_request(rid, slots=None, ref="MLS-1"):
    request = {"key": {**KEY, "site_listing_ref": ref}, "raw": {"property_type_raw": "House", "bedrooms": 3},
               "snapshot_uri": None, "slots": slots if slots is not None else {"champion": "1"}}
    return {"id": rid, "op": "score", "request": request}


def test_ping_needs_no_dependencies():
    counter = Counter()
    out = serve_all([{"id": 1, "op": "ping"}], make_factory(counter))
    assert final(out, 1)["ok"] is True and isinstance(final(out, 1)["pid"], int)
    assert counter.factory == 0


def test_dependencies_are_built_once_for_many_requests():
    counter = Counter()
    serve_all([score_request(1), score_request(2), {"id": 3, "op": "warm", "models": []}], make_factory(counter))
    assert counter.factory == 1


def test_warm_loads_the_models_and_reports_failures_per_model():
    counter = Counter()
    out = serve_all([{"id": 1, "op": "warm", "models": ["1", "9"]}], make_factory(counter, bad_ref="9"))
    loaded = final(out, 1)["loaded"]
    assert counter.loaded == ["1", "9"]
    assert isinstance(loaded["1"]["seconds"], float) and "no such version" in loaded["9"]["error"]


def test_score_returns_the_single_listing_result():
    out = serve_all([score_request(1)], make_factory(Counter()))
    msg = final(out, 1)
    assert msg["ok"] is True
    assert msg["result"]["slots"]["champion"]["pred_price_jmd"] == 3e6
    assert msg["result"]["input_source"] == "raw_fallback" and msg["result"]["inputs"]["bedrooms"] == 3


def test_a_failing_request_does_not_stop_the_server():
    counter = Counter()
    out = serve_all([score_request(1), {"id": 2, "op": "ping"}], make_factory(counter, joined_exc=RuntimeError("bq down")))
    assert final(out, 1)["ok"] is False and "bq down" in final(out, 1)["fatal"]
    assert final(out, 2)["ok"] is True


def test_unknown_op_and_bad_json_are_answered_and_skipped():
    out = serve_all([{"id": 1, "op": "dance"}, "not json {", {"id": 3, "op": "ping"}], make_factory(Counter()))
    assert final(out, 1)["ok"] is False and "dance" in final(out, 1)["fatal"]
    assert any(m.get("ok") is False and m.get("id") is None for m in out)
    assert final(out, 3)["ok"] is True


def test_a_failing_dependency_build_is_retried_on_the_next_request():
    counter = Counter()
    out = serve_all([score_request(1), score_request(2)], make_factory(counter, fail_first=True))
    assert final(out, 1)["ok"] is False and "avm_model missing" in final(out, 1)["fatal"]
    assert final(out, 2)["ok"] is True and counter.factory == 2


def test_prepare_streams_progress_then_returns_rows():
    items = [{"id": f"rec{i}", "key": {**KEY, "site_listing_ref": f"MLS-{i}"}, "raw": {"property_type_raw": "House",
             "bedrooms": i}, "snapshot_uri": None} for i in (1, 2, 3)]
    out = serve_all([{"id": 7, "op": "prepare", "items": items}], make_factory(Counter()))
    events = [m for m in out if m.get("event") == "progress"]
    assert events and all(e["id"] == 7 for e in events)
    assert {e["stage"] for e in events} >= {"lookups", "inputs"}
    msg = final(out, 7)
    assert msg["ok"] is True and [r["id"] for r in msg["rows"]] == ["rec1", "rec2", "rec3"]
    assert msg["rows"][1]["inputs"]["bedrooms"] == 2
    assert out.index(msg) == len(out) - 1  # the response closes the request


def test_predict_scores_prepared_rows_with_one_model():
    items = [{"id": f"rec{i}", "key": {**KEY, "site_listing_ref": f"MLS-{i}"}, "raw": {"property_type_raw": "House",
             "bedrooms": i}, "snapshot_uri": None} for i in (1, 2)]
    prep = final(serve_all([{"id": 1, "op": "prepare", "items": items}], make_factory(Counter())), 1)
    out = serve_all([{"id": 2, "op": "predict", "model": "1", "rows": prep["rows"]}], make_factory(Counter()))
    msg = final(out, 2)
    assert msg["ok"] is True
    assert msg["results"]["rec1"]["pred_price_jmd"] == 1e6 and msg["results"]["rec2"]["pred_price_jmd"] == 2e6


def test_shutdown_ends_the_loop():
    out = serve_all([{"id": 1, "op": "shutdown"}, {"id": 2, "op": "ping"}], make_factory(Counter()))
    assert final(out, 1)["ok"] is True
    assert not any(m.get("id") == 2 for m in out)


def test_responses_are_json_safe_for_decimals_and_numpy():
    from decimal import Decimal

    import numpy as np

    line = serve.dumps({"a": Decimal("1.5"), "b": np.float64(2.5), "c": pd.Timestamp("2026-11-01")})
    assert json.loads(line) == {"a": 1.5, "b": 2.5, "c": "2026-11-01 00:00:00"}


def test_explain_builds_the_row_and_passes_it_with_the_key():
    counter = Counter()
    req = score_request(1, slots={})
    out = serve_all([{"id": 1, "op": "explain", "request": req["request"], "model": "3"}], make_factory(counter))
    msg = final(out, 1)
    assert msg["ok"] is True and msg["result"]["drivers"][0]["group"] == "Bedrooms"
    assert msg["result"]["input_source"] == "raw_fallback"
    (ref, inputs, key), = counter.explained
    assert ref == "3" and inputs["bedrooms"] == 3 and key == KEY
