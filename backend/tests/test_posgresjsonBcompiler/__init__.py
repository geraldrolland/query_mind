"""Unit tests for app/dsl/postgresjsonBcompiler.py (PostgresJSONBCompiler).

Covers:
    - Pure helpers            (_format_decimal_value)
    - Type validation         (_validate_field_type incl. list recursion,
                               date-string parsing)
    - Plan section validators (_validate_select_fields/_filters/_metrics/
                               _group_by/_sorts)
    - SQL compilation         (select/filters/group/order/limit/HAVING,
                               parameter binding, inferred grouping,
                               whitespace normalisation)
    - Result processing       (_process_query_result)
    - DB paths                (_fetch_dataset_schema, extract_data_from_db,
                               execute_dsl) via faked Session/get_engine —
                               no live database required.

pytest does not auto-collect ``__init__.py``, so run explicitly:

    python -m pytest tests/test_posgresjsonBcompiler/__init__.py -v
"""

from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import app.dsl.postgresjsonBcompiler as pgmod
from app.dsl.postgresjsonBcompiler import PostgresJSONBCompiler
from app.schemas.query import (
    DslDefinitionSchema as QueryPlan,
    FilterSchema,
    MeasurableSchema,
    SortSchema,
)

# ===========================================================================
# Fixtures / helpers
# ===========================================================================

SCHEMA = {
    "name": {
        "type": "string",
        "allowed_operators": ["eq", "neq", "contains", "in", "nin"],
        "aggregable": False,
    },
    "amount": {
        "type": "number",
        "allowed_operators": [
            "eq", "neq", "gt", "gte", "lt", "lte",
            "between", "in", "nin",
        ],
        "aggregable": True,
    },
    "region": {
        "type": "string",
        "allowed_operators": ["eq", "neq", "contains", "in", "nin"],
        "aggregable": False,
    },
    "active": {
        "type": "boolean",
        "allowed_operators": ["eq", "neq"],
        "aggregable": False,
    },
    "created_at": {
        "type": "date",
        "allowed_operators": [
            "eq", "neq", "gt", "gte", "lt", "lte",
            "before", "after", "between", "in", "nin",
        ],
        "aggregable": False,
    },
    # Cleaner-baked derived helper column for the date column above.
    "created_at_year": {
        "type": "number",
        "allowed_operators": [
            "eq", "neq", "gt", "gte", "lt", "lte",
            "between", "in", "nin",
        ],
        "aggregable": False,
    },
}


@pytest.fixture
def compiler() -> PostgresJSONBCompiler:
    c = PostgresJSONBCompiler()
    c.schema = dict(SCHEMA)
    return c


# ===========================================================================
# Static helpers
# ===========================================================================


def test_format_decimal_value_default_precision_half_up():
    c = PostgresJSONBCompiler()
    assert c._format_decimal_value(Decimal("1.005")) == "1.01"
    assert c._format_decimal_value(Decimal("2.356")) == "2.36"
    assert c._format_decimal_value(Decimal("-1.994")) == "-1.99"


def test_format_decimal_value_custom_precision():
    c = PostgresJSONBCompiler()
    assert c._format_decimal_value(Decimal("2.567"), precision=0) == "3"
    assert c._format_decimal_value(Decimal("7.891"), precision=1) == "7.9"


# ===========================================================================
# _validate_field_type
# ===========================================================================


def test_validate_field_type_number_accepts_numeric(compiler):
    assert compiler._validate_field_type("amount", 10, "number") == 10
    assert compiler._validate_field_type("amount", 9.5, "number") == 9.5


def test_validate_field_type_number_rejects_string(compiler):
    with pytest.raises(ValueError, match="Type mismatch"):
        compiler._validate_field_type("amount", "ten", "number")


def test_validate_field_type_string_rejects_int(compiler):
    with pytest.raises(ValueError, match="Type mismatch"):
        compiler._validate_field_type("name", 42, "string")


def test_validate_field_type_boolean_rejects_truthy_int(compiler):
    # bool is strict: 1 is an int, not a bool.
    with pytest.raises(ValueError, match="Type mismatch"):
        compiler._validate_field_type("active", 1, "boolean")


def test_validate_field_type_date_parses_human_strings(compiler):
    result = compiler._validate_field_type("created_at", "March 5, 2024", "date")
    assert result == "2024-03-05T00:00:00"


def test_validate_field_type_date_unparseable_string_raises(compiler):
    with pytest.raises(ValueError, match="Type mismatch"):
        compiler._validate_field_type("created_at", "not-a-date!", "date")


def test_validate_field_type_list_validates_each_element(compiler):
    assert compiler._validate_field_type("amount", [1, 2, 3.5], "number") == [1, 2, 3.5]
    # Numeric strings are NOT coerced inside lists -> strict rejection.
    with pytest.raises(ValueError):
        compiler._validate_field_type("amount", [1, "bad", 3], "number")
    # Date strings inside date-typed lists ARE parsed.
    result = compiler._validate_field_type(
        "created_at", ["2024-01-05"], "date"
    )
    assert result == ["2024-01-05T00:00:00"]


def test_validate_field_type_unknown_expected_type_rejects_everything(compiler):
    # Unknown schema types have no allowed validators -> strict rejection.
    with pytest.raises(ValueError, match="Type mismatch"):
        compiler._validate_field_type("x", "anything", "mystery")


# ===========================================================================
# Section validators
# ===========================================================================


def test_validate_select_fields_empty_ok(compiler):
    assert compiler._validate_select_fields([], SCHEMA) is None


def test_validate_select_fields_valid_and_invalid(compiler):
    compiler._validate_select_fields(["name", "amount"], SCHEMA)  # no raise
    with pytest.raises(ValueError, match="ghost_col"):
        compiler._validate_select_fields(["name", "ghost_col"], SCHEMA)


def test_validate_filters_unknown_field(compiler):
    f = FilterSchema(field="ghost", operator="eq", value=1)
    with pytest.raises(ValueError, match="'ghost' not found"):
        compiler._validate_filters([f], SCHEMA)


def test_validate_filters_operator_not_allowed_for_type(compiler):
    f = FilterSchema(field="name", operator="gt", value="abc")
    with pytest.raises(ValueError, match="Operator 'gt' not allowed"):
        compiler._validate_filters([f], SCHEMA)


def test_validate_filters_value_type_mismatch(compiler):
    f = FilterSchema(field="amount", operator="eq", value="lots")
    with pytest.raises(ValueError, match="Type mismatch"):
        compiler._validate_filters([f], SCHEMA)


def test_validate_filters_mutates_date_string_to_iso(compiler):
    f = FilterSchema(field="created_at", operator="after", value="March 5, 2024")
    compiler._validate_filters([f], SCHEMA)
    assert f.value == "2024-03-05T00:00:00"


def test_validate_metrics_count_must_not_have_field(compiler):
    m = SimpleNamespace(metric_type="count", field="amount", alias="c")
    with pytest.raises(ValueError, match="should not specify a field"):
        compiler._validate_metrics([m], SCHEMA)


def test_validate_metrics_non_count_requires_field(compiler):
    m = SimpleNamespace(metric_type="sum", field=None, alias="s")
    with pytest.raises(ValueError, match="requires a field"):
        compiler._validate_metrics([m], SCHEMA)


def test_validate_metrics_unknown_field(compiler):
    m = SimpleNamespace(metric_type="avg", field="ghost", alias="a")
    with pytest.raises(ValueError, match="not found"):
        compiler._validate_metrics([m], SCHEMA)


def test_validate_metrics_non_aggregable_field_rejected(compiler):
    m = SimpleNamespace(metric_type="sum", field="name", alias="s")
    with pytest.raises(ValueError, match="not marked as aggregable"):
        compiler._validate_metrics([m], SCHEMA)


def test_validate_metrics_aggregable_ok(compiler):
    m = SimpleNamespace(metric_type="avg", field="amount", alias="a")
    compiler._validate_metrics([m], SCHEMA)  # no raise


def test_validate_group_by_invalid_fields_collected(compiler):
    with pytest.raises(ValueError, match="ghost_a, ghost_b"):
        compiler._validate_group_by(["ghost_a", "ghost_b"], SCHEMA)


def test_validate_sorts_metric_alias_allowed_without_schema_field(compiler):
    s = SortSchema(field="total_sales", direction="desc")
    metrics = [SimpleNamespace(alias="total_sales")]
    compiler._validate_sorts([s], SCHEMA, metrics)  # no raise


def test_validate_sorts_unknown_field_rejected(compiler):
    s = SortSchema(field="nope", direction="asc")
    with pytest.raises(ValueError, match="'nope' not found"):
        compiler._validate_sorts([s], SCHEMA, [])


# ===========================================================================
# Clause compilers
# ===========================================================================


def test_compile_select_clause_empty(compiler):
    assert compiler._compile_select_clause([]) == ""


def test_compile_select_clause_extracts_jsonb_text(compiler):
    sql = compiler._compile_select_clause(["name", "amount"])
    assert sql == "data->>'name' AS name, data->>'amount' AS amount"


def test_compile_metrics_clause_count_and_sum(compiler):
    metrics = [
        MeasurableSchema(metric_type="count", alias="rows"),
        MeasurableSchema(metric_type="sum", field="amount", alias="total"),
    ]
    sql = compiler._compile_metrics_clause(metrics)
    assert sql == "COUNT(*) AS rows, SUM((data->>'amount')::numeric) AS total"


def test_compile_metrics_clause_avg_min_max(compiler):
    metrics = [
        MeasurableSchema(metric_type="avg", field="amount", alias="a"),
        MeasurableSchema(metric_type="min", field="amount", alias="mn"),
        MeasurableSchema(metric_type="max", field="amount", alias="mx"),
    ]
    sql = compiler._compile_metrics_clause(metrics)
    assert "AVG((data->>'amount')::numeric) AS a" in sql
    assert "MIN((data->>'amount')::numeric) AS mn" in sql
    assert "MAX((data->>'amount')::numeric) AS mx" in sql


def test_compile_where_clause_base_condition_only(compiler):
    params = {}
    where, counter = compiler._compile_where_clause([], "dst_1", params)
    assert where == "datasetrow.dataset_id = :dataset_id"
    assert counter == 0
    assert params == {"dataset_id": "dst_1"}


def test_compile_where_clause_numeric_eq_casts_and_binds(compiler):
    params = {}
    f = FilterSchema(field="amount", operator="gte", value=100)
    where, counter = compiler._compile_where_clause([f], "dst_1", params)
    assert "(data->>'amount')::numeric >= :param_0" in where
    assert params["param_0"] == 100
    assert counter == 1


def test_compile_where_clause_between_two_params(compiler):
    params = {}
    f = FilterSchema(field="amount", operator="between", value=[10, 20])
    where, _ = compiler._compile_where_clause([f], "dst_1", params)
    assert "BETWEEN :param_0 AND :param_1" in where
    assert params["param_0"] == 10
    assert params["param_1"] == 20


def test_compile_where_clause_date_eq_casts_to_date(compiler):
    params = {}
    f = FilterSchema(field="created_at", operator="eq", value="2024-11-01T00:00:00")
    where, counter = compiler._compile_where_clause([f], "dst_1", params)
    assert "(data->>'created_at')::date = :param_0" in where
    assert params["param_0"] == "2024-11-01"
    assert counter == 1


def test_compile_where_clause_date_between_casts_to_date(compiler):
    params = {}
    f = FilterSchema(field="created_at", operator="between", value=["2024-01-01T00:00:00", "2024-12-31T00:00:00"])
    where, _ = compiler._compile_where_clause([f], "dst_1", params)
    assert "BETWEEN :param_0 AND :param_1" in where
    assert params["param_0"] == "2024-01-01"
    assert params["param_1"] == "2024-12-31"


def test_compile_where_clause_date_in_casts_to_date(compiler):
    params = {}
    f = FilterSchema(field="created_at", operator="in", value=["2024-01-01T00:00:00", "2024-06-15T00:00:00"])
    where, _ = compiler._compile_where_clause([f], "dst_1", params)
    assert "IN (:param_0, :param_1)" in where
    assert params["param_0"] == "2024-01-01"
    assert params["param_1"] == "2024-06-15"


@pytest.mark.parametrize(
    ("operator", "value"),
    [("between", []), ("between", [1]), ("in", []), ("in", "x"), ("nin", [])],
)
def test_compile_where_clause_list_operators_require_lists(compiler, operator, value):
    f = FilterSchema(field="amount", operator=operator, value=value)
    with pytest.raises(ValueError, match=f"'{operator}'"):
        compiler._compile_where_clause([f], "dst_1", {})


def test_compile_where_clause_in_generates_placeholders(compiler):
    params = {}
    f = FilterSchema(field="region", operator="in", value=["east", "west"])
    where, counter = compiler._compile_where_clause([f], "dst_1", params)
    assert "(data->>'region') IN (:param_0, :param_1)" in where
    assert [params["param_0"], params["param_1"]] == ["east", "west"]
    assert counter == 2


def test_compile_where_clause_nin_generates_not_in(compiler):
    f = FilterSchema(field="region", operator="nin", value=["east"])
    where, _ = compiler._compile_where_clause([f], "dst_1", {})
    assert "NOT IN (:param_0)" in where


def test_compile_where_clause_contains_builds_like(compiler):
    params = {}
    f = FilterSchema(field="name", operator="contains", value="li")
    where, _ = compiler._compile_where_clause([f], "dst_1", params)
    assert "(data->>'name') LIKE '%' || :param_0 || '%'" in where
    assert params["param_0"] == "li"


def test_compile_where_clause_unmapped_operator_raises(compiler):
    # FilterSchema's Literal operator prevents constructing this via pydantic,
    # but the compiler keeps a defensive guard for direct calls.
    f = SimpleNamespace(field="amount", operator="weird", value=1)
    with pytest.raises(ValueError, match="Unmapped operator 'weird'"):
        compiler._compile_where_clause([f], "dst_1", {})


def test_compile_group_by_clause_empty(compiler):
    assert compiler._compile_group_by_clause([]) == ""


def test_compile_group_by_clause_plain_field(compiler):
    assert compiler._compile_group_by_clause(["region"]) == "GROUP BY (data->>'region')"


def test_compile_order_by_numeric_cast_and_direction(compiler):
    s = SortSchema(field="amount", direction="desc")
    sql = compiler._compile_order_by_clause([s], has_metrics=False)
    assert sql == "ORDER BY (data->>'amount')::numeric DESC"


def test_compile_order_by_string_field_no_cast(compiler):
    s = SortSchema(field="name", direction="asc")
    sql = compiler._compile_order_by_clause([s], has_metrics=False)
    assert sql == "ORDER BY (data->>'name') ASC"


def test_compile_order_by_metric_alias_nulls_last(compiler):
    s = SortSchema(field="total_sales", direction="desc")
    sql = compiler._compile_order_by_clause([s], has_metrics=True)
    assert sql == "ORDER BY total_sales DESC NULLS LAST"


# ===========================================================================
# compile() end-to-end (no database)
# ===========================================================================


def _plan(**kwargs) -> QueryPlan:
    return QueryPlan(**kwargs)


def test_compile_simple_select(compiler):
    plan = _plan(select=["name", "amount"])
    sql, params = compiler.compile(plan, "dst_42")
    assert sql.startswith("SELECT data->>'name' AS name, data->>'amount' AS amount")
    assert "FROM datasetrow" in sql
    assert "WHERE datasetrow.dataset_id = :dataset_id" in sql
    assert params == {"dataset_id": "dst_42"}
    assert "GROUP BY" not in sql
    assert sql == " ".join(sql.split())  # whitespace normalized


def test_compile_count_metric_only(compiler):
    plan = _plan(metrics=[MeasurableSchema(metric_type="count", alias="rows")])
    sql, _ = compiler.compile(plan, "dst_1")
    assert sql.startswith("SELECT COUNT(*) AS rows")
    # Metrics without group_by infer grouping over nothing -> no GROUP BY.
    assert "GROUP BY" not in sql


def test_compile_select_plus_metric_infers_grouping(compiler):
    plan = _plan(
        select=["region"],
        metrics=[MeasurableSchema(metric_type="sum", field="amount", alias="total")],
    )
    sql, _ = compiler.compile(plan, "dst_1")
    assert "(data->>'region') AS region" in sql
    assert "SUM((data->>'amount')::numeric) AS total" in sql
    assert "GROUP BY (data->>'region')" in sql


def test_compile_grouped_with_metrics_emits_having_guard(compiler):
    plan = _plan(
        select=["region"],
        group_by=["region"],
        metrics=[
            MeasurableSchema(metric_type="sum", field="amount", alias="total"),
            MeasurableSchema(metric_type="count", alias="rows"),
        ],
    )
    sql, _ = compiler.compile(plan, "dst_1")
    # SUM gets a HAVING NULL guard; COUNT(*) does not.
    assert "HAVING SUM((data->>'amount')::numeric) IS NOT NULL" in sql
    assert "COUNT(*) IS NOT NULL" not in sql


def test_compile_filters_bind_parameters(compiler):
    plan = _plan(
        select=["name"],
        filters=[
            FilterSchema(field="amount", operator="lt", value=500),
            FilterSchema(field="region", operator="eq", value="east"),
        ],
    )
    sql, params = compiler.compile(plan, "dst_7")
    assert "(data->>'amount')::numeric < :param_0" in sql
    assert "(data->>'region') = :param_1" in sql
    assert params["param_0"] == 500
    assert params["param_1"] == "east"
    assert "AND" in sql


def test_compile_limit_parameterized(compiler):
    plan = _plan(select=["name"], limit=25)
    sql, params = compiler.compile(plan, "dst_1")
    assert sql.endswith("LIMIT :limit")
    assert params["limit"] == 25


def test_compile_without_limit_has_no_limit_clause(compiler):
    plan = _plan(select=["name"])
    sql, params = compiler.compile(plan, "dst_1")
    assert "LIMIT" not in sql
    assert "limit" not in params


def test_compile_degrades_gracefully_without_schema():
    bare = PostgresJSONBCompiler()  # schema never fetched -> falsy {}
    plan = _plan(select=["anything"])
    sql, _ = bare.compile(plan, "dst_1")
    # Unknown fields fall back to string handling (no ::numeric cast).
    assert "(data->>'anything') ASC" not in sql
    assert "data->>'anything' AS anything" in sql


# ===========================================================================
# _process_query_result
# ===========================================================================


def test_process_query_result_formats_decimals_datetimes_and_passthrough(compiler):
    raw = [
        {
            "total": Decimal("123.456"),
            "exact": Decimal("5"),
            "when": datetime(2024, 6, 1, 13, 30),
            "label": "east",
            "n": 7,
            "flag": True,
            "nothing": None,
        }
    ]
    processed = compiler._process_query_result(raw)
    assert processed == [
        {
            "total": "123.46",
            "exact": "5.00",
            "when": "2024-06-01T13:30:00",
            "label": "east",
            "n": 7,
            "flag": True,
            "nothing": None,
        }
    ]


# ===========================================================================
# DB-backed paths (faked Session / get_engine)
# ===========================================================================


class FakeMappings:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return self.rows


class FakeSession:
    """Stands in for sqlmodel.Session; pops queued row batches per open."""

    row_queue: list = []

    def __init__(self, engine=None):
        self.engine = engine
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, clause):
        self.executed.append(str(clause))
        return FakeMappings(FakeSession.row_queue.pop(0))


@pytest.fixture
def faked_db(monkeypatch):
    monkeypatch.setattr(pgmod, "get_engine", lambda: object())
    monkeypatch.setattr(pgmod, "Session", FakeSession)
    return FakeSession


def test_fetch_dataset_schema_returns_schema(faked_db):
    faked_db.row_queue = [[{"dataset_schema": dict(SCHEMA)}]]
    c = PostgresJSONBCompiler()
    assert c._fetch_dataset_schema(7) == SCHEMA


def test_fetch_dataset_schema_missing_dataset_raises(faked_db):
    faked_db.row_queue = [[]]
    c = PostgresJSONBCompiler()
    with pytest.raises(ValueError, match="not found"):
        c._fetch_dataset_schema(999)


def test_fetch_dataset_schema_empty_schema_raises(faked_db):
    faked_db.row_queue = [[{"dataset_schema": {}}]]
    c = PostgresJSONBCompiler()
    with pytest.raises(ValueError, match="no schema defined"):
        c._fetch_dataset_schema(8)


def test_extract_data_from_db_processes_multiple_queries(faked_db, compiler):
    faked_db.row_queue = [
        [{"total": Decimal("10.005"), "region": "east"}],
        [{"when": datetime(2024, 1, 2), "n": 3}],
    ]
    results = compiler.extract_data_from_db(
        [("SELECT :a", {"a": 1}), ("SELECT :b", {"b": 2})]
    )
    assert results == [
        [{"total": "10.01", "region": "east"}],
        [{"when": "2024-01-02T00:00:00", "n": 3}],
    ]
    assert len(faked_db.row_queue) == 0


def test_extract_data_from_db_reraises_errors(faked_db, compiler, monkeypatch):
    class ExplodingSession(FakeSession):
        def execute(self, clause):
            raise RuntimeError("boom")

    monkeypatch.setattr(pgmod, "Session", ExplodingSession)
    with pytest.raises(RuntimeError, match="boom"):
        compiler.extract_data_from_db([("SELECT 1", {})])


def test_execute_dsl_orchestrates_validate_compile_extract(monkeypatch, compiler):
    calls = []

    def fake_validate(plan, dataset_id):
        calls.append(("validate", dataset_id))

    def fake_compile(plan, dataset_id):
        calls.append(("compile", dataset_id))
        return "SELECT 1", {"dataset_id": dataset_id}

    def fake_extract(queries):
        calls.append(("extract", queries))
        return [[{"rows": 1}]]

    monkeypatch.setattr(compiler, "validate_dsl", fake_validate)
    monkeypatch.setattr(compiler, "compile", fake_compile)
    monkeypatch.setattr(compiler, "extract_data_from_db", fake_extract)

    result = compiler.execute_dsl({"select": ["name"]}, "dst_9")

    assert result == [[{"rows": 1}]]
    assert [step[0] for step in calls] == ["validate", "compile", "extract"]
    assert calls[0][1] == "dst_9"
    # Raw dict input was converted into a QueryPlan internally.
    assert calls[2][1] == [("SELECT 1", {"dataset_id": "dst_9"})]


def test_execute_dsl_accepts_plan_instances(monkeypatch, compiler):
    seen = {}

    def fake_validate(plan, dataset_id):
        seen["is_plan"] = isinstance(plan, QueryPlan)

    monkeypatch.setattr(compiler, "validate_dsl", fake_validate)
    monkeypatch.setattr(compiler, "compile", lambda plan, did: ("SELECT 1", {}))
    monkeypatch.setattr(compiler, "extract_data_from_db", lambda q: [[{}]])

    compiler.execute_dsl(QueryPlan(select=["name"]), "dst_1")
    assert seen["is_plan"] is True


def test_execute_dsl_propagates_validation_errors(faked_db, compiler):
    # Schema fetch succeeds, but the plan references an unknown select field.
    faked_db.row_queue = [[{"dataset_schema": dict(SCHEMA)}]]
    with pytest.raises(ValueError, match="ghost"):
        compiler.execute_dsl({"select": ["ghost"]}, "dst_1")


def test_execute_dsl_rejects_invalid_dicts_via_pydantic(compiler):
    with pytest.raises(ValidationError):
        compiler.execute_dsl({"select": [], "filters": []}, "dst_1")
