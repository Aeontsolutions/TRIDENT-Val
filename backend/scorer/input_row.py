"""Assemble one model input row from a ``listing_clean`` join or from raw listing fields.

Plain dict code: no pandas and no ``avm_model`` import. The model's column list is
passed in as ``columns``.
"""

import re
from collections.abc import Mapping, Sequence
from typing import Any

# Blocking flags from the listing_clean data dictionary.
CLEAN_FLAG_COLUMNS: tuple[str, ...] = (
    "price_missing_or_unparseable",
    "currency_unsupported",
    "fx_rate_missing",
    "floor_area_implausible",
    "bedrooms_implausible",
    "bathrooms_implausible",
    "price_per_sqft_extreme",
    "unknown_label",
)


def _clean(value: Any) -> Any:
    """NaN and pandas-style NA scalars become None."""
    if value is None:
        return None
    try:
        if value != value:  # NaN; pd.NA raises on bool() below
            return None
    except (TypeError, ValueError):
        return None
    return value


def row_from_joined(
    joined: Mapping[str, Any], page: Mapping[str, Any], columns: Sequence[str]
) -> dict[str, Any]:
    """Page values win over the joined row; absent or NaN values are None."""
    return {
        col: _clean(page[col] if col in page else joined.get(col)) for col in columns
    }


def _float_or_none(value: Any) -> float | None:
    value = _clean(value)
    return None if value is None else float(value)


# Copied from packages/avm_features/src/avm_features/listing_rules.py (cleaning_version listing-clean-v2):
# PROPERTY_TYPE_MAP, CANONICAL_PARISHES, _text, _parish_key, _title_case and the "By Private Treaty" community rule. They must be kept in step with it,
# because this module is tested without avm_features and the row has to be built the way training builds it.
PROPERTY_TYPE_MAP = {
    "House": "house",
    "Apartment": "apartment",
    "Townhouse": "townhouse",
}

CANONICAL_PARISHES = [
    "Kingston & St. Andrew",
    "St. Ann",
    "St. James",
    "St. Mary",
    "St. Catherine",
    "Manchester",
    "Trelawny",
    "Westmoreland",
    "St. Thomas",
    "Clarendon",
    "St. Elizabeth",
    "Hanover",
    "Portland",
]


def _text(value: Any) -> str | None:
    """A string with its whitespace trimmed and collapsed; None for a missing or blank value."""
    if not isinstance(value, str):
        return None
    return " ".join(value.split()) or None


def _parish_key(value: Any) -> str | None:
    """Spelling-insensitive key: case, full stops and whitespace ignored, 'and' read as '&'."""
    text = _text(value)
    if text is None:
        return None
    return " ".join(text.lower().replace(".", " ").replace(" and ", " & ").split())


_PARISH_BY_KEY = {_parish_key(p): p for p in CANONICAL_PARISHES}


def _title_case(value: Any) -> str | None:
    text = _text(value)
    return None if text is None else " ".join(w[:1].upper() + w[1:].lower() for w in text.split(" "))


_PRIVATE_TREATY_RE = re.compile(r"^\s*by\s+private\s+treaty\b\s*", re.IGNORECASE)


def _community(value: Any) -> str | None:
    """The community as listing_clean has it: a leading "By Private Treaty" removed, then title-cased."""
    text = _text(value)
    if text is None:
        return None
    return _title_case(_PRIVATE_TREATY_RE.sub("", text, count=1))


def _raw_base(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Raw listing keys mapped onto listing_clean column names and units."""
    unit = _clean(raw.get("floor_area_unit"))
    sqft = unit is not None and str(unit).strip().lower() == "sqft"
    return {
        "property_type": PROPERTY_TYPE_MAP.get(_text(raw.get("property_type_raw"))),
        "bedrooms": _clean(raw.get("bedrooms")),
        "bathrooms": _clean(raw.get("bathrooms")),
        "floor_area_sqft": _float_or_none(raw.get("floor_area")) if sqft else None,
        "parish": _PARISH_BY_KEY.get(_parish_key(raw.get("parish_raw"))),
        "community": _community(raw.get("community")),
        "latitude": _clean(raw.get("latitude")),
        "longitude": _clean(raw.get("longitude")),
    }


def row_from_raw(
    raw: Mapping[str, Any],
    geo: Mapping[str, Any] | None,
    spatial: Mapping[str, Any] | None,
    page: Mapping[str, Any],
    columns: Sequence[str],
) -> dict[str, Any]:
    """Row for a listing that is not in listing_clean. Later sources win: raw, geo, spatial, page."""
    merged: dict[str, Any] = {}
    for source in (_raw_base(raw), geo or {}, spatial or {}, page):
        merged.update(source)
    return {col: _clean(merged.get(col)) for col in columns}


def blocking_flags(joined: Mapping[str, Any]) -> list[str]:
    """Names in CLEAN_FLAG_COLUMNS that are true."""
    return [name for name in CLEAN_FLAG_COLUMNS if _clean(joined.get(name))]
