from app.model_limits import LOCATION_FEATURE_COLUMNS, flagged_fields, geo_missing, input_warnings, location_features_missing


def test_bedrooms_over_limit_warns():
    warnings = input_warnings({"bedrooms": 45.0, "bathrooms": 2.0})
    assert len(warnings) == 1
    assert warnings[0]["field"] == "bedrooms"
    assert warnings[0]["code"] == "input_out_of_range"


def test_in_range_and_missing_do_not_warn():
    assert input_warnings({"bedrooms": 8.0, "bathrooms": None}) == []


def test_flagged_fields():
    assert flagged_fields({"bedrooms": 45.0, "bathrooms": 60.0}) == {
        "bedrooms": "outlier",
        "bathrooms": "outlier",
    }


def test_geo_missing():
    assert geo_missing({"latitude": None, "longitude": 1.0}) is True
    assert geo_missing({"latitude": 18.0, "longitude": -76.8}) is False


def test_location_features_missing_needs_coordinates_and_no_features():
    coords = {"latitude": 18.0, "longitude": -76.8}
    assert location_features_missing(coords) is True
    assert location_features_missing({**coords, "dist_coast_m": None, "elevation_m": None}) is True
    assert location_features_missing({**coords, "dist_coast_m": 0.0}) is False  # zero is a value, not missing
    assert location_features_missing({**coords, "nearest_town_name": "Richmond Park"}) is False
    assert location_features_missing({"latitude": None, "longitude": None}) is False  # geo_missing covers this
    assert location_features_missing({}) is False


def test_location_feature_columns_are_the_spatial_and_geo_inputs():
    assert len(LOCATION_FEATURE_COLUMNS) == 16
    assert {"dist_coast_m", "road_km_1km", "n_school_500m", "nearest_town_name"} <= set(LOCATION_FEATURE_COLUMNS)
    assert not {"latitude", "longitude"} & set(LOCATION_FEATURE_COLUMNS)
