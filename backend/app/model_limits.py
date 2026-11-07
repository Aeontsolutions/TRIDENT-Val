"""Plausibility limits for the model-estimate inputs.

Floor-area plausibility is not here: the model reports it as ``floor_area_flagged``.
"""

MAX_BEDROOMS = 8
MAX_BATHROOMS = 8

_LIMITS: dict[str, int] = {
    "bedrooms": MAX_BEDROOMS,
    "bathrooms": MAX_BATHROOMS,
}


def _out_of_range(inputs: dict[str, object]) -> list[tuple[str, int]]:
    """(field, limit) for each limited field whose value is present and above its limit."""
    found = []
    for field, limit in _LIMITS.items():
        value = inputs.get(field)
        if isinstance(value, (int, float)) and value > limit:
            found.append((field, limit))
    return found


def input_warnings(inputs: dict[str, object]) -> list[dict]:
    """One warning per out-of-range field. Missing values do not warn."""
    return [
        {
            "code": "input_out_of_range",
            "field": field,
            "message": f"{field} is above {limit}; the estimate may be unreliable.",
        }
        for field, limit in _out_of_range(inputs)
    ]


def flagged_fields(inputs: dict[str, object]) -> dict[str, str]:
    """Out-of-range fields mapped to ``"outlier"``."""
    return {field: "outlier" for field, _ in _out_of_range(inputs)}


# Copied from packages/avm_model/src/avm_model/prototype/columns.py (SPATIAL + GEO): the model inputs computed from the
# coordinates. Keep in step with it.
LOCATION_FEATURE_COLUMNS: tuple[str, ...] = (
    # SPATIAL
    "dist_nearest_town_m",
    "nearest_town_name",
    "n_school_500m",
    "n_school_1km",
    "dist_nearest_school_m",
    "n_church_500m",
    "n_church_1km",
    "dist_nearest_church_m",
    "dist_nearest_hospital_m",
    "n_supermarket_1km",
    "dist_nearest_supermarket_m",
    # GEO
    "dist_coast_m",
    "elevation_m",
    "slope_deg",
    "road_km_500m",
    "road_km_1km",
)


def geo_missing(inputs: dict[str, object]) -> bool:
    """True when latitude or longitude is missing."""
    return inputs.get("latitude") is None or inputs.get("longitude") is None


def location_features_missing(inputs: dict[str, object]) -> bool:
    """True when the coordinates are present but every location feature (distances, counts, elevation, roads) is missing."""
    if geo_missing(inputs):
        return False
    return all(inputs.get(col) is None for col in LOCATION_FEATURE_COLUMNS)
