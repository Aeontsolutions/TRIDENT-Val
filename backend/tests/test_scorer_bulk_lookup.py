"""The pure parts of the bulk BigQuery lookups used by the batch view (the queries themselves need BigQuery)."""
from scorer.score_listing import GEO_SPATIAL_MANY_SQL, GEO_SPATIAL_SQL, KEY_IN_SQL, group_by_key


def row(ref, **extra):
    return {"source_site_id": "s", "site_listing_ref": ref, **extra}


def test_group_by_key_maps_each_key_to_its_row():
    out = group_by_key([row("A", x=1), row("B", x=2)])
    assert out[("s", "A")] == row("A", x=1) and out[("s", "B")]["x"] == 2


def test_group_by_key_turns_duplicate_rows_into_the_error_the_single_lookup_raises():
    out = group_by_key([row("A", x=1), row("B", x=2), row("B", x=3)])
    assert isinstance(out[("s", "B")], ValueError) and "2 rows for s/B" in str(out[("s", "B")])
    assert out[("s", "A")]["x"] == 1


def test_group_by_key_can_drop_the_key_columns():
    assert group_by_key([row("A", x=1)], drop_key=True) == {("s", "A"): {"x": 1}}


def test_bulk_geo_query_keeps_the_raw_coordinates_like_the_single_lookup():
    # Training keeps listing_clean's coordinates, so neither lookup may let these tables override latitude/longitude.
    assert "EXCEPT (latitude, longitude)" in GEO_SPATIAL_MANY_SQL
    assert "latitude, longitude" in GEO_SPATIAL_SQL
    assert "UNNEST(@keys)" in KEY_IN_SQL and "{keys}" in GEO_SPATIAL_MANY_SQL
