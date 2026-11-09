"""Bulk input-row building and multi-row prediction for the batch view. Fakes only: no network, no avm_model."""
import threading

import pandas as pd

from scorer import batch
from scorer.score_listing import Deps

COLUMNS = ["property_type", "bedrooms", "pool", "dist_coast_m"]


def key(i):
    return {"source_site_id": "s", "site_listing_ref": f"MLS-{i}"}


def item(i, **over):
    base = {
        "id": f"rec{i}",
        "key": key(i),
        "raw": {"property_type_raw": "House", "bedrooms": i},
        "snapshot_uri": f"gs://b/d/{i}.html.gz",
    }
    base.update(over)
    return base


def joined(i):
    return {"record_id": f"clean{i}", "fetched_at": "2026-11-01T00:00:00", "trainable": False, "fx_rate_used": 1.0,
            "price_jmd": 1e7, "property_type": "house", "bedrooms": i, "pool": None, "dist_coast_m": 10.0 * i}


class Calls:
    def __init__(self):
        self.joined_many = []
        self.geo_many = []
        self.pages = []
        self.joined_one = []
        self.geo_one = []
        self.lock = threading.Lock()


def make_deps(clean_ids=(1,), page_fail=(), calls=None, bulk=True, duplicate=()):
    calls = calls if calls is not None else Calls()

    def fetch_page_fields(uri):
        with calls.lock:
            calls.pages.append(uri)
        if any(uri.endswith(f"/{i}.html.gz") for i in page_fail):
            raise OSError(f"no page for {uri}")
        return {"pool": 1.0}

    def joined_many(keys):
        calls.joined_many.append(list(keys))
        out = {}
        for k in keys:
            i = int(k[1].split("-")[1])
            if i in duplicate:
                out[k] = ValueError(f"2 rows for {k[0]}/{k[1]}")
            elif i in clean_ids:
                out[k] = joined(i)
        return out

    def geo_many(keys):
        calls.geo_many.append(list(keys))
        return {k: ({"dist_coast_m": 5.0}, None) for k in keys}

    def joined_one(site, ref):
        calls.joined_one.append((site, ref))
        i = int(ref.split("-")[1])
        return joined(i) if i in clean_ids else None

    def geo_one(site, ref):
        calls.geo_one.append((site, ref))
        return ({"dist_coast_m": 5.0}, None)

    deps = Deps(
        columns=lambda: list(COLUMNS),
        fetch_joined=joined_one,
        fetch_geo_spatial=geo_one,
        fetch_page_fields=fetch_page_fields,
        load_model=lambda ref: None,
        categorical=lambda: ["property_type"],
        fetch_joined_many=joined_many if bulk else None,
        fetch_geo_spatial_many=geo_many if bulk else None,
    )
    return deps, calls


def by_id(rows):
    return {r["id"]: r for r in rows}


# ---------- prepare_rows ----------


def test_prepare_uses_one_bulk_lookup_per_table_and_marks_the_source():
    deps, calls = make_deps(clean_ids=(1,))
    rows = by_id(batch.prepare_rows([item(1), item(2), item(3)], deps))
    assert len(calls.joined_many) == 1 and len(calls.joined_many[0]) == 3
    assert len(calls.geo_many) == 1 and len(calls.geo_many[0]) == 2  # only the rows that are not in listing_clean
    assert calls.joined_one == [] and calls.geo_one == []
    assert rows["rec1"]["input_source"] == "listing_clean" and rows["rec1"]["clean"]["record_id"] == "clean1"
    assert rows["rec2"]["input_source"] == "raw_fallback" and rows["rec2"]["clean"] is None
    assert rows["rec2"]["inputs"]["property_type"] == "house" and rows["rec2"]["inputs"]["bedrooms"] == 2
    assert rows["rec2"]["inputs"]["dist_coast_m"] == 5.0  # from the geo fallback
    assert rows["rec1"]["inputs"]["pool"] == 1.0  # page fields win over the joined row
    assert all(r["error"] is None for r in rows.values())


def test_prepare_fetches_each_distinct_page_once():
    deps, calls = make_deps()
    same = item(2, snapshot_uri="gs://b/d/1.html.gz")
    batch.prepare_rows([item(1), same, item(3, snapshot_uri=None)], deps)
    assert calls.pages == ["gs://b/d/1.html.gz"]


def test_prepare_page_failure_is_reported_for_that_row_only():
    deps, _ = make_deps(page_fail=(2,))
    rows = by_id(batch.prepare_rows([item(1), item(2), item(3)], deps))
    assert rows["rec2"]["page_error"] and "no page" in rows["rec2"]["page_error"]
    assert rows["rec1"]["page_error"] is None and rows["rec3"]["page_error"] is None
    assert rows["rec2"]["inputs"] is not None  # still scored without the page fields


def test_prepare_duplicate_clean_rows_become_a_row_error():
    deps, _ = make_deps(duplicate=(2,))
    rows = by_id(batch.prepare_rows([item(1), item(2)], deps))
    assert "2 rows for" in rows["rec2"]["error"] and rows["rec2"]["inputs"] is None
    assert rows["rec1"]["error"] is None


def test_prepare_falls_back_to_per_listing_lookups_without_bulk_functions():
    deps, calls = make_deps(clean_ids=(1,), bulk=False)
    rows = by_id(batch.prepare_rows([item(1), item(2)], deps))
    assert sorted(calls.joined_one) == [("s", "MLS-1"), ("s", "MLS-2")]
    assert calls.geo_one == [("s", "MLS-2")]
    assert rows["rec1"]["input_source"] == "listing_clean" and rows["rec2"]["input_source"] == "raw_fallback"


def test_prepare_reports_progress_to_completion():
    deps, _ = make_deps()
    seen = []
    batch.prepare_rows([item(i) for i in range(1, 6)], deps, progress=lambda stage, done, total: seen.append((stage, done, total)))
    stages = {s for s, _, _ in seen}
    assert {"pages", "inputs"} <= stages
    for stage in ("pages", "inputs"):
        counts = [d for s, d, _ in seen if s == stage]
        assert counts == sorted(counts) and counts[-1] == 5
    assert all(t == 5 for _, _, t in seen)


def test_prepare_handles_an_empty_list():
    deps, calls = make_deps()
    assert batch.prepare_rows([], deps) == []
    assert calls.joined_many == []


# ---------- predict_rows ----------


class EchoModel:
    """Price = bedrooms * 1,000,000, one output row per input row; records every frame it was given."""

    def __init__(self, label="m/1", poison=None):
        self.label, self.poison, self.frames = label, poison, []

    def predict(self, frame):
        self.frames.append(frame)
        if self.poison is not None and (frame["bedrooms"] == self.poison).any():
            raise RuntimeError("poison row")
        p = frame["bedrooms"].to_numpy() * 1e6
        return pd.DataFrame({
            "pred_price_jmd": p, "lower_80_jmd": p * 0.8, "upper_80_jmd": p * 1.2, "lower_90_jmd": p * 0.7,
            "upper_90_jmd": p * 1.3, "lower_95_jmd": p * 0.6, "upper_95_jmd": p * 1.4,
            "floor_area_flagged": [False] * len(p), "model_version": [self.label] * len(p),
        })


def prepared(n):
    deps, _ = make_deps(clean_ids=())
    return batch.prepare_rows([item(i) for i in range(1, n + 1)], deps)


def test_predict_rows_scores_all_rows_in_one_model_call():
    model = EchoModel()
    deps, _ = make_deps()
    deps.load_model = lambda ref: model
    out = batch.predict_rows("1", prepared(4), deps)
    assert len(model.frames) == 1 and len(model.frames[0]) == 4
    assert list(model.frames[0].columns) == COLUMNS
    assert model.frames[0]["bedrooms"].dtype == "float64"
    assert [out["results"][f"rec{i}"]["pred_price_jmd"] for i in range(1, 5)] == [1e6, 2e6, 3e6, 4e6]
    assert out["results"]["rec1"]["label"] == "m/1" and out["results"]["rec1"]["floor_area_flagged"] is False


def test_predict_rows_skips_rows_that_could_not_be_prepared():
    model = EchoModel()
    deps, _ = make_deps(duplicate=(2,), clean_ids=())
    deps.load_model = lambda ref: model
    rows = batch.prepare_rows([item(1), item(2), item(3)], deps)
    out = batch.predict_rows("1", rows, deps)
    assert len(model.frames[0]) == 2
    assert set(out["results"]) == {"rec1", "rec3"}


def test_predict_rows_model_load_failure_marks_every_row():
    deps, _ = make_deps()

    def load(ref):
        raise KeyError("no such version")

    deps.load_model = load
    out = batch.predict_rows("9", prepared(3), deps)
    assert set(out["results"]) == {"rec1", "rec2", "rec3"}
    assert all("no such version" in r["error"] for r in out["results"].values())


def test_predict_rows_isolates_a_row_the_model_rejects():
    model = EchoModel(poison=3.0)
    deps, _ = make_deps()
    deps.load_model = lambda ref: model
    out = batch.predict_rows("1", prepared(5), deps)
    assert out["results"]["rec3"] == {"error": "poison row"}
    assert [out["results"][f"rec{i}"]["pred_price_jmd"] for i in (1, 2, 4, 5)] == [1e6, 2e6, 4e6, 5e6]


def test_predict_rows_reports_progress():
    deps, _ = make_deps()
    deps.load_model = lambda ref: EchoModel()
    seen = []
    batch.predict_rows("1", prepared(3), deps, progress=lambda stage, done, total: seen.append((stage, done, total)))
    assert seen[0][0] == "model" and seen[-1] == ("predict", 3, 3)
