import pandas as pd

from scorer.score_listing import Deps, run

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
