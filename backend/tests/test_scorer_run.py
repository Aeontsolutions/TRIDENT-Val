from decimal import Decimal

import pandas as pd

from scorer.score_listing import GEO_SPATIAL_SQL, Deps, _frame, run

COLUMNS = ["property_type", "bedrooms", "pool", "dist_coast_m"]
KEY = {"source_site_id": "cbjamaica.com", "site_listing_ref": "MLS-1"}
JOINED = {
    "record_id": "r1",
    "fetched_at": "2026-11-01T00:00:00",
    "trainable": True,
    "fx_rate_used": 1.0,
    "price_jmd": 30_000_000.0,
    "property_type": "apartment",
    "bedrooms": 2,
    "pool": None,
    "dist_coast_m": 150.0,
}


class FakeModel:
    def __init__(self, price: float, label: str):
        self.price = price
        self.label = label
        self.seen = None

    def predict(self, frame):
        self.seen = frame
        p = self.price
        return pd.DataFrame(
            [
                {
                    "pred_price_jmd": p,
                    "lower_80_jmd": p * 0.8,
                    "upper_80_jmd": p * 1.2,
                    "lower_90_jmd": p * 0.7,
                    "upper_90_jmd": p * 1.3,
                    "lower_95_jmd": p * 0.6,
                    "upper_95_jmd": p * 1.4,
                    "floor_area_flagged": False,
                    "model_version": self.label,
                }
            ]
        )


class BoomModel:
    def predict(self, frame):
        raise RuntimeError("boom")


def make_deps(joined=JOINED, models=None, page=None, page_exc=None):
    models = models or {
        "champion": FakeModel(30_000_000.0, "m/1"),
        "challenger": FakeModel(32_000_000.0, "m/2"),
    }

    def fetch_page_fields(uri):
        if page_exc:
            raise page_exc
        return page or {}

    return Deps(
        columns=lambda: list(COLUMNS),
        fetch_joined=lambda s, r: dict(joined) if joined is not None else None,
        fetch_geo_spatial=lambda s, r: (None, None),
        fetch_page_fields=fetch_page_fields,
        load_model=lambda ref: models[ref],
        categorical=lambda: ["property_type"],
    )


def request(champion="champion", challenger="challenger", snapshot_uri=None, raw=None):
    return {
        "key": KEY,
        "raw": raw if raw is not None else {},
        "snapshot_uri": snapshot_uri,
        "slots": {"champion": champion, "challenger": challenger},
    }


def test_scores_both_slots_and_returns_inputs():
    response = run(request(), make_deps())
    assert response["input_source"] == "listing_clean"
    assert response["slots"]["champion"]["pred_price_jmd"] == 30_000_000.0
    assert response["slots"]["champion"]["label"] == "m/1"
    assert response["slots"]["challenger"]["pred_price_jmd"] == 32_000_000.0
    assert set(response["inputs"]) == set(COLUMNS)
    assert response["page_error"] is None


def test_challenger_failure_keeps_champion():
    models = {"champion": FakeModel(30_000_000.0, "m/1"), "challenger": BoomModel()}
    response = run(request(), make_deps(models=models))
    assert response["slots"]["challenger"] == {"error": "boom"}
    assert response["slots"]["champion"]["pred_price_jmd"] == 30_000_000.0


def test_model_load_failure_is_a_slot_error():
    deps = make_deps()

    def load(ref):
        if ref == "challenger":
            raise KeyError("no such alias")
        return FakeModel(1.0, "m/1")

    deps.load_model = load
    response = run(request(), deps)
    assert "error" in response["slots"]["challenger"]
    assert response["slots"]["champion"]["pred_price_jmd"] == 1.0


def test_not_in_clean_uses_raw_fallback():
    raw = {"property_type_raw": "House", "bedrooms": 3}
    response = run(request(raw=raw), make_deps(joined=None))
    assert response["input_source"] == "raw_fallback"
    assert response["clean"] is None
    assert response["inputs"]["property_type"] == "house"
    assert response["inputs"]["bedrooms"] == 3


def test_zero_slots_still_returns_inputs():
    response = run(request(champion=None, challenger=None), make_deps())
    assert response["slots"] == {}
    assert response["inputs"]["property_type"] == "apartment"


def test_page_failure_is_reported_not_fatal():
    deps = make_deps(page_exc=RuntimeError("gcloud failed"))
    response = run(request(snapshot_uri="gs://b/2026-11-01/MLS-1.html.gz"), deps)
    assert response["page_error"] == "gcloud failed"
    assert response["inputs"]["pool"] is None
    assert response["slots"]["champion"]["pred_price_jmd"] == 30_000_000.0


def test_page_fields_override_joined_inputs():
    deps = make_deps(page={"pool": 1.0})
    response = run(request(snapshot_uri="gs://b/2026-11-01/MLS-1.html.gz"), deps)
    assert response["inputs"]["pool"] == 1.0


def test_page_not_fetched_without_snapshot_uri():
    deps = make_deps(page_exc=RuntimeError("must not be called"))
    response = run(request(snapshot_uri=None), deps)
    assert response["page_error"] is None


def test_clean_block_reports_blocking_flags():
    joined = {**JOINED, "bedrooms_implausible": True, "trainable": False}
    response = run(request(), make_deps(joined=joined))
    assert response["clean"]["blocking_flags"] == ["bedrooms_implausible"]
    assert response["clean"]["trainable"] is False
    assert response["clean"]["record_id"] == "r1"
    assert response["clean"]["price_jmd"] == 30_000_000.0


def test_clean_block_reports_the_original_currency():
    response = run(request(), make_deps(joined={**JOINED, "price_currency_original": "USD", "fx_rate_used": 158.0}))
    assert response["clean"]["price_currency_original"] == "USD"
    assert response["clean"]["fx_rate_used"] == 158.0


def test_geo_spatial_query_keeps_the_raw_coordinates():
    """Training keeps listing_clean's coordinates; the fallback must not take the feature tables' copy instead."""
    sql = GEO_SPATIAL_SQL.format(project="p", dataset="d", table="listing_geo_features")
    assert "EXCEPT (source_site_id, site_listing_ref, latitude, longitude)" in sql
    assert "`p.d.listing_geo_features`" in sql and "WHERE source_site_id = @s AND site_listing_ref = @r" in sql


def test_frame_is_one_row_in_column_order():
    frame = _frame({"b": 1, "a": 2, "extra": 3}, ["a", "b", "c"], set())
    assert list(frame.columns) == ["a", "b", "c"]
    assert len(frame) == 1
    assert list(frame.index) == [0]


def test_frame_numeric_columns_are_float64():
    frame = _frame({"n": 3, "x": 1.5}, ["n", "x"], set())
    assert (frame.dtypes == "float64").all()
    assert frame["n"].iloc[0] == 3.0


def test_frame_decimal_bool_and_none_become_float64():
    frame = _frame(
        {"d": Decimal("1.25"), "flag": True, "off": False, "missing": None},
        ["d", "flag", "off", "missing", "absent"],
        set(),
    )
    assert (frame.dtypes == "float64").all()
    assert frame["d"].iloc[0] == 1.25
    assert frame["flag"].iloc[0] == 1.0
    assert frame["off"].iloc[0] == 0.0
    assert pd.isna(frame["missing"].iloc[0])
    assert pd.isna(frame["absent"].iloc[0])


def test_frame_categorical_stays_a_string():
    frame = _frame({"property_type": "apartment", "n": 1}, ["property_type", "n"], {"property_type"})
    assert frame["property_type"].iloc[0] == "apartment"
    assert frame["n"].dtype == "float64"


def test_frame_unparseable_numeric_string_is_nan():
    frame = _frame({"n": "not a number", "m": "12.5"}, ["n", "m"], set())
    assert pd.isna(frame["n"].iloc[0])
    assert frame["m"].iloc[0] == 12.5


def test_run_hands_the_model_the_cast_frame():
    model = FakeModel(30_000_000.0, "m/1")
    joined = {**JOINED, "bedrooms": Decimal("2"), "pool": True}
    deps = make_deps(joined=joined, models={"champion": model})
    run(request(challenger=None), deps)
    seen = model.seen
    assert list(seen.columns) == COLUMNS
    assert len(seen) == 1
    assert seen["property_type"].iloc[0] == "apartment"
    assert seen["bedrooms"].dtype == "float64" and seen["bedrooms"].iloc[0] == 2.0
    assert seen["pool"].dtype == "float64" and seen["pool"].iloc[0] == 1.0
    assert seen["dist_coast_m"].dtype == "float64"
