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


def geo_missing(inputs: dict[str, object]) -> bool:
    """True when latitude or longitude is missing."""
    return inputs.get("latitude") is None or inputs.get("longitude") is None
