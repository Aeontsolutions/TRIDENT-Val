"""The single-listing panel converts a USD listing outside the clean table with the rate the scorer looked up."""
from test_model_check import FakeRunner, env, score, scorer_response  # noqa: F401 - env is a fixture


def raw_fallback_with(fx):
    return scorer_response(input_source="raw_fallback", clean=None, fx=fx)


def test_usd_outside_the_clean_table_gets_a_jmd_asking_price_and_a_gap(env):  # noqa: F811
    env["runner"] = FakeRunner(raw_fallback_with({"rate": 160.0, "date": "2026-11-06"}))
    body = score(env, "rec_house").json()  # USD 1,000,000 in the fixture
    assert body["asking"]["jmd"] == 160_000_000.0
    assert body["asking"]["fx"] == {"rate": 160.0, "date": "2026-11-06", "source": "Bank of Jamaica mid rate"}
    assert round(body["slots"]["champion"]["gap_pct"], 1) == round((612e6 / 160e6 - 1) * 100, 1)
    warning = next(w for w in body["warnings"] if w["code"] == "not_in_clean_table")
    assert "Bank of Jamaica mid rate of 2026-11-06" in warning["message"] and "no JMD asking price" not in warning["message"]


def test_without_a_looked_up_rate_nothing_changes(env):  # noqa: F811
    env["runner"] = FakeRunner(raw_fallback_with(None))
    body = score(env, "rec_house").json()
    assert body["asking"]["jmd"] is None and "fx" not in body["asking"]
    warning = next(w for w in body["warnings"] if w["code"] == "not_in_clean_table")
    assert "no JMD asking price or gap" in warning["message"]


def test_a_rate_is_ignored_for_a_jmd_listing(env):  # noqa: F811
    env["runner"] = FakeRunner(raw_fallback_with({"rate": 160.0, "date": "2026-11-06"}))
    body = score(env, "rec_apt").json()
    assert body["asking"]["jmd"] == 850_000_000 and "fx" not in body["asking"]


def test_the_clean_tables_own_rate_wins_over_a_looked_up_one(env):  # noqa: F811
    clean = scorer_response()["clean"] | {"record_id": "rec_house", "fx_rate_used": 150.0, "price_currency_original": "USD"}
    env["runner"] = FakeRunner(scorer_response(clean=clean, fx={"rate": 999.0, "date": "2026-11-06"}))
    assert score(env, "rec_house").json()["asking"]["jmd"] == 150_000_000.0
