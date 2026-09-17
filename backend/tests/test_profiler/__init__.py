"""Unit tests for app/profiler/profiler.py (Dataset Profiler).

Covers:
    - _coerce      (numeric / date-string coercion, blanks, passthroughs)
    - profile()    (unknown dataset, empty schema, full profiling pipeline,
                    DSL filter scoping, limit handling, quality metrics,
                    sample-row truncation)

Database access is faked via patched ``Session``/``get_engine``; the DSL
compiler is stubbed so no queries run.

pytest does not auto-collect ``__init__.py``, so run explicitly:

    python -m pytest tests/test_profiler/__init__.py -v
"""

from datetime import date, datetime

import pytest

import app.profiler.profiler as prof
from app.models.dataset import Dataset
from app.profiler.profiler import Profiler, _SAMPLE_LIMIT

# _coerce is an instance method; exercise it through a throwaway profiler.
_coerce = lambda value: Profiler(dataset_id="dst_test")._coerce(value)

# ===========================================================================
# Fakes
# ===========================================================================


class FakeSession:
    """Stands in for sqlmodel.Session; session.get returns a preset dataset."""

    dataset_to_return = None
    instances: list = []

    def __init__(self, engine=None):
        self.engine = engine
        FakeSession.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, model, pk):
        return FakeSession.dataset_to_return


class FakeCompiler:
    def __init__(self):
        self.calls = []

    def execute_dsl(self, plan, dataset_id):
        self.calls.append((plan, dataset_id))
        return self.results

    results: list = []


@pytest.fixture
def faked_db(monkeypatch):
    FakeSession.instances = []
    FakeSession.dataset_to_return = None
    monkeypatch.setattr(prof, "get_engine", lambda: object())
    monkeypatch.setattr(prof, "Session", FakeSession)
    return FakeSession


@pytest.fixture
def fake_compiler(monkeypatch):
    compiler = FakeCompiler()
    monkeypatch.setattr(prof.jsonBcompiler, "execute_dsl", compiler.execute_dsl)
    return compiler


def make_dataset(schema: dict, total_rows: int = 100) -> Dataset:
    return Dataset(user_id=1, name="ds", id="dst_1",
                   dataset_schema=schema, total_rows=total_rows)


# ===========================================================================
# _coerce
# ===========================================================================


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (7, 7),
        (3.14, 3.14),
        (True, True),
        ("42", 42),
        ("-5", -5),
        ("3.14", 3.14),
        ("2024-03-15", datetime(2024, 3, 15)),
        ("2024-03-15T10:30:45", datetime(2024, 3, 15, 10, 30, 45)),
        ("  spaced  ", "spaced"),  # stripped, not numeric/date -> text
        ("hello", "hello"),
        ("", None),
        ("   ", None),
    ],
)
def test_coerce(value, expected):
    result = _coerce(value)
    assert result == expected
    assert type(result) is type(expected)


def test_coerce_passthrough_datetime_and_date_objects():
    dt = datetime(2024, 1, 2, 3, 4, 5)
    d = date(2024, 1, 2)
    assert _coerce(dt) is dt
    assert _coerce(d) is d


# ===========================================================================
# profile() — degenerate inputs
# ===========================================================================


def test_profile_unknown_dataset_yields_empty_document(faked_db, fake_compiler):
    FakeSession.dataset_to_return = None
    report = Profiler(dataset_id="dst_missing").profile()

    assert report == {
        "dataset_id": "dst_missing",
        "row_count": 0,
        "columns": [],
        "quality": {"columns_with_nulls": 0},
        "sample_rows": [],
    }
    assert fake_compiler.calls == []  # compiler never invoked


def test_profile_dataset_without_schema_skips_compilation(faked_db, fake_compiler):
    FakeSession.dataset_to_return = make_dataset(schema={}, total_rows=55)
    report = Profiler(dataset_id="dst_1").profile()

    assert report["row_count"] == 55  # still reports stored total
    assert report["columns"] == []
    assert fake_compiler.calls == []


# ===========================================================================
# profile() — full pipeline
# ===========================================================================

SCHEMA = {
    "name": {"type": "string"},
    "amount": {"type": "number", "aggregable": True},
    "joined": {"type": "date"},
}

ROWS = [
    {"name": "alpha", "amount": 10, "joined": "2024-01-15"},
    {"name": "beta", "amount": 20, "joined": "2024-02-20"},
    {"name": "alpha", "amount": None, "joined": None},
    {"name": "gamma", "amount": 60, "joined": "2024-01-15"},
]


@pytest.fixture
def profiled(faked_db, fake_compiler) -> dict:
    FakeSession.dataset_to_return = make_dataset(SCHEMA, total_rows=400)
    fake_compiler.results = [ROWS]
    return Profiler(dataset_id="dst_1").profile()


def test_profile_requests_all_schema_columns_via_dsl(profiled, fake_compiler):
    plan, dataset_id = fake_compiler.calls[0]
    assert dataset_id == "dst_1"
    assert plan["select"] == ["name", "amount", "joined"]
    assert plan["filters"] == []
    assert plan["limit"] == _SAMPLE_LIMIT


def test_profile_row_count_from_dataset_total_when_unfiltered(profiled):
    assert profiled["row_count"] == 400


def test_profile_number_column_stats(profiled):
    amount = next(c for c in profiled["columns"] if c["name"] == "amount")
    assert amount["type"] == "number"
    assert amount["min"] == 10
    assert amount["max"] == 60
    assert amount["avg"] == 30.0  # (10 + 20 + 60) / 3


def test_profile_string_column_top_values(profiled):
    name = next(c for c in profiled["columns"] if c["name"] == "name")
    assert name["type"] == "string"
    # alpha appears twice -> ranked first.
    assert name["top_values"][0] == "alpha"
    assert set(name["top_values"]) >= {"beta", "gamma"}


def test_profile_date_column_min_max_isoformatted(profiled):
    joined = next(c for c in profiled["columns"] if c["name"] == "joined")
    assert joined["min"] == "2024-01-15T00:00:00"
    assert joined["max"] == "2024-02-20T00:00:00"


def test_profile_null_counts_and_quality(profiled):
    by_name = {c["name"]: c for c in profiled["columns"]}
    assert by_name["name"]["nulls"] == 0
    assert by_name["amount"]["nulls"] == 1
    assert by_name["joined"]["nulls"] == 1
    assert profiled["quality"] == {"columns_with_nulls": 2}


def test_profile_sample_rows_capped_at_ten(faked_db, fake_compiler):
    FakeSession.dataset_to_return = make_dataset(SCHEMA, total_rows=50)
    fake_compiler.results = [[{"n": i} for i in range(25)]]
    report = Profiler(dataset_id="dst_1").profile()
    assert len(report["sample_rows"]) == 10
    assert report["sample_rows"][9] == {"n": 9}


def test_profile_empty_result_rows_handled(faked_db, fake_compiler):
    FakeSession.dataset_to_return = make_dataset(SCHEMA, total_rows=0)
    fake_compiler.results = [[]]
    report = Profiler(dataset_id="dst_1").profile()
    assert all(c["nulls"] == 0 for c in report["columns"])
    assert report["sample_rows"] == []
    assert report["row_count"] == 0


# ===========================================================================
# profile() — DSL scoping and limits
# ===========================================================================


def test_profile_passes_filters_and_overrides_row_count(faked_db, fake_compiler):
    FakeSession.dataset_to_return = make_dataset(SCHEMA, total_rows=999)
    filtered_rows = ROWS[:2]
    fake_compiler.results = [filtered_rows]

    report = Profiler(
        dataset_id="dst_1",
        dsl_definition={"filters": [{"field": "amount", "operator": "gte", "value": 10}]},
    ).profile()

    plan, _ = fake_compiler.calls[0]
    assert plan["filters"] == [
        {"field": "amount", "operator": "gte", "value": 10}
    ]
    # Filtered profiles report the matched row count, not the table total.
    assert report["row_count"] == 2


def test_profile_uses_dsl_limit_when_provided(faked_db, fake_compiler):
    FakeSession.dataset_to_return = make_dataset(SCHEMA)
    Profiler(dataset_id="dst_1", dsl_definition={"limit": 50}).profile()
    plan, _ = fake_compiler.calls[0]
    assert plan["limit"] == 50


def test_profile_defaults_limit_when_dsl_limit_falsy(faked_db, fake_compiler):
    FakeSession.dataset_to_return = make_dataset(SCHEMA)
    Profiler(dataset_id="dst_1", dsl_definition={}).profile()
    plan, _ = fake_compiler.calls[0]
    assert plan["limit"] == _SAMPLE_LIMIT


def test_profile_handles_none_results_list(faked_db, fake_compiler):
    FakeSession.dataset_to_return = make_dataset(SCHEMA)
    fake_compiler.results = []
    report = Profiler(dataset_id="dst_1").profile()
    assert report["columns"][0]["nulls"] == 0
    assert report["row_count"] == make_dataset(SCHEMA).total_rows


# ===========================================================================
# profile() — type-specific edge behaviour
# ===========================================================================


def test_profile_boolean_column_gets_top_values_not_stats(faked_db, fake_compiler):
    schema = {"active": {"type": "boolean"}}
    FakeSession.dataset_to_return = make_dataset(schema, total_rows=3)
    fake_compiler.results = [
        [{"active": True}, {"active": False}, {"active": True}]
    ]
    report = Profiler(dataset_id="dst_1").profile()
    active = report["columns"][0]
    assert active["type"] == "boolean"
    assert active["top_values"] == ["True", "False"]
    assert "min" not in active and "max" not in active


def test_profile_number_column_without_values_has_no_stats(faked_db, fake_compiler):
    schema = {"amount": {"type": "number"}}
    FakeSession.dataset_to_return = make_dataset(schema)
    fake_compiler.results = [[{"amount": None}, {"amount": None}]]
    report = Profiler(dataset_id="dst_1").profile()
    amount = report["columns"][0]
    assert "min" not in amount and "max" not in amount and "avg" not in amount
    assert amount["nulls"] == 2
