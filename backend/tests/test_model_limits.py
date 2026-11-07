from app.model_limits import flagged_fields, geo_missing, input_warnings


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
