"""Unit tests for app/cleaning/cleaner.py (CSV cleaning pipeline).

Covers:
    - CleaningResult      (dataclass defaults)
    - _infer_type         (dtype-based + object-column coercion rules)
    - _normalize_float    (integral floats -> int)
    - _parse_date_part    (datetime / ISO-string extraction)
    - enrich_dataset      (derived _year/_month/_day columns)
    - clean_csv           (end-to-end: parsing, dedupe, nulls, schema)

pytest does not auto-collect ``__init__.py``, so run explicitly:

    python -m pytest tests/test_cleaning/__init__.py -v
"""

import pandas as pd
import pytest

from app.cleaning.cleaner import (
    OPERATORS_BY_TYPE,
    TYPE_MAP,
    CleaningResult,
    _infer_type,
    _normalize_float,
    _parse_date_part,
    clean_csv,
    enrich_dataset,
)

# ===========================================================================
# Constants / dataclass
# ===========================================================================


def test_type_map_covers_pandas_dtypes():
    assert TYPE_MAP["int64"] == "number"
    assert TYPE_MAP["float64"] == "number"
    assert TYPE_MAP["object"] == "string"
    assert TYPE_MAP["bool"] == "boolean"
    assert TYPE_MAP["datetime64[ns]"] == "date"


def test_cleaning_result_defaults_are_independent():
    a = CleaningResult(raw_rows=1, rows_ingested=1, duplicates_removed=0)
    b = CleaningResult(raw_rows=1, rows_ingested=1, duplicates_removed=0)
    assert a.null_counts == {} and a.columns == {} and a.rows == []
    a.null_counts["x"] = 1
    a.rows.append({"k": "v"})
    assert b.null_counts == {}
    assert b.rows == []


# ===========================================================================
# _infer_type
# ===========================================================================


def test_infer_bool_dtype():
    assert _infer_type(pd.Series([True, False, True])) == "boolean"


def test_infer_int_dtype_is_number():
    assert _infer_type(pd.Series([1, 2, 3])) == "number"


def test_infer_float_dtype_is_number():
    assert _infer_type(pd.Series([1.5, 2.5, 3.5])) == "number"


def test_infer_datetime_dtype_is_date():
    s = pd.Series(pd.to_datetime(["2024-01-01", "2024-06-15"]))
    assert _infer_type(s) == "date"


def test_infer_object_numeric_strings_coerce_to_number():
    assert _infer_type(pd.Series(["1", "22", "333"])) == "number"


def test_infer_object_iso_dates_coerce_to_date():
    s = pd.Series(["2024-01-01", "2024-06-15", "2023-12-31"])
    assert _infer_type(s) == "date"


def test_infer_all_null_series_defaults_to_string():
    # read_csv yields an all-NaN float64 column for fully empty CSV columns;
    # with no values there is no type evidence, so it must stay "string".
    assert _infer_type(pd.Series([None, None])) == "string"
    assert _infer_type(pd.Series([float("nan")])) == "string"


def test_infer_plain_strings_stay_string():
    assert _infer_type(pd.Series(["alpha", "beta", "gamma"])) == "string"


def test_infer_short_numeric_sample_below_three_stays_string():
    # Fewer than 3 non-null samples -> no coercion attempted.
    assert _infer_type(pd.Series(["1", "2"])) == "string"


def test_infer_object_eighty_percent_boundary():
    # Exactly 4/5 = 0.80 numeric values meets the >= 0.8 threshold.
    assert _infer_type(pd.Series(["1", "2", "3", "4", "oops"])) == "number"


def test_infer_object_below_threshold_with_unparseable_junk_is_string():
    # 3/4 = 0.75 numeric -> below threshold; junk is not date-like either.
    assert _infer_type(pd.Series(["1", "2", "3", "oops"])) == "string"


# ===========================================================================
# _normalize_float
# ===========================================================================


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (2.0, 2),
        (-7.0, -7),
        (2.5, 2.5),
        ("text", "text"),
        (3, 3),
        (None, None),
        (True, True),
    ],
)
def test_normalize_float(value, expected):
    assert _normalize_float(value) == expected
    assert type(_normalize_float(value)) is type(expected)


# ===========================================================================
# _parse_date_part
# ===========================================================================


def test_parse_date_part_none_value_returns_none():
    assert _parse_date_part(None, "year") is None
    assert _parse_date_part(None, "month") is None
    assert _parse_date_part(None, "day") is None


def test_parse_date_part_from_datetime_object():
    import datetime as dt

    d = dt.datetime(2024, 3, 15)
    assert _parse_date_part(d, "year") == 2024
    assert _parse_date_part(d, "month") == 3
    assert _parse_date_part(d, "day") == 15


def test_parse_date_part_from_iso_string():
    assert _parse_date_part("2024-03-15", "year") == 2024
    assert _parse_date_part("2024-03-15", "month") == 3
    assert _parse_date_part("2024-03-15", "day") == 15


def test_parse_date_part_truncates_full_iso_datetime_string():
    assert _parse_date_part("2024-03-15T10:30:45", "year") == 2024
    assert _parse_date_part("2024-03-15T10:30:45", "day") == 15


def test_parse_date_part_invalid_string_returns_none():
    assert _parse_date_part("not-a-date", "year") is None


# ===========================================================================
# enrich_dataset
# ===========================================================================


def _schema_entry(dtype):
    return {"type": dtype, "allowed_operators": OPERATORS_BY_TYPE[dtype]}


def test_enrich_dataset_adds_year_month_day_columns():
    schema = {
        "joined": _schema_entry("date"),
        "amount": _schema_entry("number"),
    }
    rows = [
        {"joined": "2021-05-14", "amount": 100},
        {"joined": "2022-12-01", "amount": 200},
    ]
    enrich_dataset(rows, schema)

    for part, expected in (("year", 2021), ("month", 5), ("day", 14)):
        assert rows[0][f"joined_{part}"] == expected
    assert rows[1]["joined_year"] == 2022

    # Non-date columns are untouched.
    assert "amount_year" not in schema

    # Derived columns are typed as number, share number operators, never aggregable.
    for part in ("year", "month", "day"):
        rule = schema[f"joined_{part}"]
        assert rule["type"] == "number"
        assert rule["aggregable"] is False
        assert rule["allowed_operators"] == OPERATORS_BY_TYPE["number"]


def test_enrich_dataset_null_or_missing_dates_become_none():
    schema = {"joined": _schema_entry("date")}
    rows = [{"joined": None}, {"amount": 1}]  # second row lacks the key
    enrich_dataset(rows, schema)
    assert rows[0]["joined_year"] is None
    assert rows[1]["joined_year"] is None
    assert rows[1]["joined_day"] is None


def test_enrich_dataset_skips_existing_derived_columns():
    schema = {
        "joined": _schema_entry("date"),
        "joined_year": {"type": "string"},  # pre-existing, must be preserved
    }
    rows = [{"joined": "2020-02-29"}]
    enrich_dataset(rows, schema)
    assert schema["joined_year"] == {"type": "string"}  # untouched
    # But month/day were still added.
    assert schema["joined_month"]["type"] == "number"
    assert schema["joined_day"]["type"] == "number"


def test_enrich_dataset_noop_without_date_columns():
    schema = {"name": _schema_entry("string")}
    rows = [{"name": "x"}]
    enrich_dataset(rows, schema)
    assert list(schema.keys()) == ["name"]
    assert rows == [{"name": "x"}]


# ===========================================================================
# clean_csv — error paths
# ===========================================================================


def test_clean_csv_empty_file_raises():
    with pytest.raises(ValueError, match="no rows"):
        clean_csv(b"a,b\n")


def test_clean_csv_garbage_bytes_raise_parse_error():
    with pytest.raises(ValueError, match="Failed to parse CSV"):
        clean_csv(b"\x00\xff not,a@csv\x00\x01")


def test_clean_csv_rejects_whitespace_only_column_name():
    # NOTE: read_csv auto-renames truly empty headers ("Unnamed: 0"),
    # so the guard only catches blank-but-present names like " ".
    with pytest.raises(ValueError, match="empty name"):
        clean_csv(b" ,age\n1,2\n")


# ===========================================================================
# clean_csv — happy paths
# ===========================================================================

BASIC_CSV = (
    " name ,age,joined,active,score\n"
    "Alice,34,2021-05-14,True,1200.50\n"
    "Bob,28,2020-11-02,False,980.0\n"
    "Cara,41,2022-01-30,True,1050.25\n"
    "Bob,28,2020-11-02,False,980.0\n"  # exact duplicate of previous row
)


@pytest.fixture(scope="module")
def basic_result() -> CleaningResult:
    return clean_csv(BASIC_CSV.encode("utf-8"), filename="basic.csv")


def test_clean_csv_column_names_are_stripped(basic_result):
    assert set(basic_result.columns.keys()) == {"name", "age", "joined", "active", "score"}


def test_clean_csv_deduplicates_exact_rows(basic_result):
    assert basic_result.raw_rows == 4
    assert basic_result.duplicates_removed == 1
    assert basic_result.rows_ingested == 3
    assert len(basic_result.rows) == 3


def test_clean_csv_infers_types(basic_result):
    assert basic_result.columns == {
        "name": "string",
        "age": "number",
        "joined": "date",
        "active": "boolean",
        "score": "number",
    }


def test_clean_csv_boolean_column_not_aggregable(basic_result):
    rule = basic_result.schema["active"]
    assert rule["type"] == "boolean"
    assert rule["aggregable"] is False
    assert rule["allowed_operators"] == ["eq", "neq"]
    cara = next(r for r in basic_result.rows if r["name"] == "Cara")
    assert cara["active"] is True


def test_clean_csv_schema_flags_and_operators(basic_result):
    schema = basic_result.schema
    for col, dtype in basic_result.columns.items():
        rule = schema[col]
        assert rule["type"] == dtype
        assert rule["allowed_operators"] == OPERATORS_BY_TYPE[dtype]
        assert rule["aggregable"] is (dtype == "number")


def test_clean_csv_normalizes_integral_floats_in_rows(basic_result):
    bob = next(r for r in basic_result.rows if r["name"] == "Bob")
    assert bob["score"] == 980
    assert isinstance(bob["score"], int)
    alice = next(r for r in basic_result.rows if r["name"] == "Alice")
    assert alice["score"] == 1200.5  # non-integral float preserved


def test_clean_csv_keeps_booleans_and_strings_json_safe(basic_result):
    cara = next(r for r in basic_result.rows if r["name"] == "Cara")
    assert cara["active"] is True
    assert cara["name"] == "Cara"


def test_clean_csv_enriches_date_columns(basic_result):
    schema = basic_result.schema
    alice = next(r for r in basic_result.rows if r["name"] == "Alice")
    assert (alice["joined_year"], alice["joined_month"], alice["joined_day"]) == (
        2021,
        5,
        14,
    )
    for part in ("year", "month", "day"):
        derived = f"joined_{part}"
        assert derived in schema
        assert schema[derived]["type"] == "number"
        assert schema[derived]["aggregable"] is False


def test_clean_csv_whitespace_cells_count_as_nulls():
    result = clean_csv(
        "name,note\nAlice,\nBob,   \n".encode("utf-8")
    )
    assert result.null_counts == {"name": 0, "note": 2}
    assert result.rows[0]["note"] is None
    assert result.rows[1]["note"] is None


def test_clean_csv_rows_contain_none_not_nan_for_missing_values():
    result = clean_csv("a,b\n1,\n".encode("utf-8"))
    row = result.rows[0]
    assert row["b"] is None
    # JSON-safe: no NaN floats anywhere in the row (NaN != NaN).
    assert not any(isinstance(v, float) and v != v for v in row.values())


def test_clean_csv_all_null_column_stays_string():
    result = clean_csv("a,b\n1,\n2,\n".encode("utf-8"))
    assert result.columns["b"] == "string"
