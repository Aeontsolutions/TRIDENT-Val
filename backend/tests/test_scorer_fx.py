"""Bank of Jamaica rates for USD listings that are not in listing_clean (they have no rate of their own)."""
from scorer import batch
from scorer.score_listing import build_row

from test_scorer_batch import make_deps


def usd_item(i, currency="USD", fetched_at="2026-11-08T07:30:00+00:00"):
    return {"id": f"rec{i}", "key": {"source_site_id": "s", "site_listing_ref": f"MLS-{i}"},
            "raw": {"property_type_raw": "House", "bedrooms": i, "currency": currency, "amount": 100_000,
                    "fetched_at": fetched_at},
            "snapshot_uri": None}


def with_fx(deps, fx=None, fail=False):
    calls = []

    def fx_rates(timestamps):
        calls.append(list(timestamps))
        if fail:
            raise RuntimeError("fx table unreadable")
        return [fx for _ in timestamps]

    deps.fx_rates = fx_rates
    return calls


RATE = {"rate": 157.5, "date": "2026-11-06"}


def test_prepare_looks_up_one_rate_per_usd_listing_outside_the_clean_table_in_one_call():
    deps, _ = make_deps(clean_ids=(1,))
    calls = with_fx(deps, RATE)
    items = [usd_item(1), usd_item(2), usd_item(3, currency="JMD"), usd_item(4, fetched_at="2026-11-07T10:00:00+00:00")]
    rows = {r["id"]: r for r in batch.prepare_rows(items, deps)}
    assert calls == [["2026-11-08T07:30:00+00:00", "2026-11-07T10:00:00+00:00"]]  # rec2 and rec4 only, one call
    assert rows["rec2"]["fx"] == RATE and rows["rec4"]["fx"] == RATE
    assert rows["rec1"]["fx"] is None  # in listing_clean: it carries its own rate
    assert rows["rec3"]["fx"] is None  # JMD needs none


def test_a_listing_with_no_rate_in_range_keeps_fx_none():
    deps, _ = make_deps(clean_ids=())
    with_fx(deps, None)
    assert batch.prepare_rows([usd_item(1)], deps)[0]["fx"] is None


def test_a_failing_rate_lookup_does_not_fail_the_batch():
    deps, _ = make_deps(clean_ids=())
    with_fx(deps, fail=True)
    rows = batch.prepare_rows([usd_item(1), usd_item(2)], deps)
    assert [r["fx"] for r in rows] == [None, None] and [r["error"] for r in rows] == [None, None]


def test_without_a_rates_function_fx_is_none_and_no_lookup_is_attempted():
    deps, _ = make_deps(clean_ids=())
    assert deps.fx_rates is None
    assert batch.prepare_rows([usd_item(1)], deps)[0]["fx"] is None


def test_one_listing_gets_its_rate_too_and_listings_in_the_clean_table_do_not():
    deps, _ = make_deps(clean_ids=(1,))
    calls = with_fx(deps, RATE)
    raw_only = build_row({"key": usd_item(2)["key"], "raw": usd_item(2)["raw"], "snapshot_uri": None}, deps)
    assert raw_only["input_source"] == "raw_fallback" and raw_only["fx"] == RATE
    in_clean = build_row({"key": usd_item(1)["key"], "raw": usd_item(1)["raw"], "snapshot_uri": None}, deps)
    assert in_clean["input_source"] == "listing_clean" and in_clean["fx"] is None
    assert calls == [["2026-11-08T07:30:00+00:00"]]
