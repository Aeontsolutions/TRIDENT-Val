from scorer.input_row import (
    CLEAN_FLAG_COLUMNS,
    blocking_flags,
    row_from_joined,
    row_from_raw,
)

RAW = {
    "property_type_raw": "Apartment",
    "bedrooms": 2,
    "bathrooms": 1,
    "floor_area": 27025,
    "floor_area_unit": "sqft",
    "parish_raw": "Kingston & St. Andrew",
    "community": "  OCHO   RIOS ",
    "latitude": 18.4,
    "longitude": -77.1,
}


def test_row_from_joined_takes_page_fields_over_joined():
    row = row_from_joined(
        {"bedrooms": 3, "pool": None},
        {"pool": 1.0, "lot_size_sqft": 5000.0},
        ["bedrooms", "pool", "lot_size_sqft", "dist_coast_m"],
    )
    assert row == {
        "bedrooms": 3,
        "pool": 1.0,
        "lot_size_sqft": 5000.0,
        "dist_coast_m": None,
    }


def test_nan_becomes_none():
    row = row_from_joined({"bedrooms": float("nan")}, {}, ["bedrooms"])
    assert row == {"bedrooms": None}


def test_row_from_raw_maps_types_and_units():
    cols = ["property_type", "floor_area_sqft", "community", "parish", "bedrooms"]
    row = row_from_raw(RAW, None, None, {}, cols)
    assert row["property_type"] == "apartment"
    assert row["floor_area_sqft"] == 27025.0
    assert row["community"] == "Ocho Rios"
    assert row["parish"] == "Kingston & St. Andrew"
    assert row["bedrooms"] == 2

    sqm = row_from_raw({**RAW, "floor_area_unit": "sqm"}, None, None, {}, cols)
    assert sqm["floor_area_sqft"] is None


def test_row_from_raw_uses_geo_and_spatial_when_given():
    cols = ["dist_coast_m", "dist_road_m", "pool"]
    row = row_from_raw(
        RAW, {"dist_coast_m": 120.0}, {"dist_road_m": 40.0}, {"pool": 1.0}, cols
    )
    assert row == {"dist_coast_m": 120.0, "dist_road_m": 40.0, "pool": 1.0}

    empty = row_from_raw(RAW, None, None, {}, cols)
    assert empty == {"dist_coast_m": None, "dist_road_m": None, "pool": None}


def test_blocking_flags():
    joined = {"bedrooms_implausible": True, "unknown_label": False, "fx_rate_missing": None}
    assert blocking_flags(joined) == ["bedrooms_implausible"]
    assert len(CLEAN_FLAG_COLUMNS) == 8
