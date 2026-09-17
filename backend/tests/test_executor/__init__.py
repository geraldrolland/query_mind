"""Unit tests for app/mcp/executor.py (MCP tool implementations).

Covers:
    - _dsl_key             (stable canonical hashing)
    - _load_plan           (dict validation + friendly error mapping)
    - _format_plan_error   (select/group_by/generic error branches)
    - execute_query_tool   (redis caching, dataset lookup, compiler call)
    - validate_dsl_tool    (row truncation, messaging)
    - get_profile_tool /
      _cache_get_profile   (module-level TTL cache behaviour)

Database access is faked via patched ``Session``/``get_engine``; Redis via a
recording fake; the profiler class is replaced so no queries run.

pytest does not auto-collect ``__init__.py``, so run explicitly:

    python -m pytest tests/test_executor/__init__.py -v
"""

import json
import time
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import app.mcp.executor as ex
from app.mcp.executor import (
    _cache_get_profile,
    _dsl_key,
    _format_plan_error,
    _load_plan,
    _PROFILE_TTL,
    execute_query_tool,
    get_profile_tool,
    validate_dsl_tool,
)
from app.models.dataset import Dataset
from app.schemas.query import DslDefinitionSchema

# ===========================================================================
# Helpers / fakes
# ===========================================================================


class FakeQueryResult:
    def __init__(self, first):
        self._first = first

    def first(self):
        return self._first


class FakeSession:
    """Stands in for sqlmodel.Session; returns a preset dataset."""

    instances: list = []
    dataset_to_return = None
    fail_exec = False

    def __init__(self, engine=None):
        self.engine = engine
        FakeSession.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def exec(self, query):
        if FakeSession.fail_exec:
            raise RuntimeError("db exploded")
        return FakeQueryResult(FakeSession.dataset_to_return)


class FakeRedis:
    def __init__(self, hit_value=None):
        self.hit_value = hit_value
        self.get_calls = []
        self.set_calls = []

    def get(self, key):
        self.get_calls.append(key)
        return self.hit_value

    def set(self, key, value, ex=None):
        self.set_calls.append((key, value, ex))


class FakeProfiler:
    """Replaces app.profiler.profiler.Profiler; counts instantiations."""

    instances: list = []
    report: dict = {"columns": [], "rows": []}

    def __init__(self, dataset_id=None, dsl_definition=None):
        self.dataset_id = dataset_id
        self.dsl_definition = dsl_definition
        FakeProfiler.instances.append(self)

    def profile(self):
        return dict(FakeProfiler.report)


@pytest.fixture(autouse=True)
def clean_profile_cache(monkeypatch):
    monkeypatch.setattr(ex, "_profile_cache", {})
    FakeProfiler.instances = []
    monkeypatch.setattr("app.profiler.profiler.Profiler", FakeProfiler)


@pytest.fixture
def faked_db(monkeypatch):
    FakeSession.instances = []
    FakeSession.dataset_to_return = None
    FakeSession.fail_exec = False
    monkeypatch.setattr(ex, "get_engine", lambda: object())
    monkeypatch.setattr(ex, "Session", FakeSession)
    return FakeSession


@pytest.fixture
def fake_redis(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(ex, "redis_client", redis)
    return redis


def patch_compiler(monkeypatch, rows):
    calls = []

    def fake_execute_dsl(plan, dataset_id):
        calls.append((plan, dataset_id))
        return [rows]

    monkeypatch.setattr(ex.jsonBcompiler, "execute_dsl", fake_execute_dsl)
    return calls


# ===========================================================================
# _dsl_key
# ===========================================================================


def test_dsl_key_none_is_literal():
    assert _dsl_key(None) == "none"


def test_dsl_key_is_sha256_hex():
    key = _dsl_key({"select": ["a"]})
    assert len(key) == 64
    int(key, 16)  # hex


def test_dsl_key_order_independent():
    a = _dsl_key({"select": ["a"], "filters": [], "limit": 5})
    b = _dsl_key({"limit": 5, "filters": [], "select": ["a"]})
    assert a == b


def test_dsl_key_differs_for_different_plans():
    assert _dsl_key({"select": ["a"]}) != _dsl_key({"select": ["b"]})


# ===========================================================================
# _load_plan
# ===========================================================================


def test_load_plan_accepts_valid_dict():
    plan = _load_plan({"select": ["name"]})
    assert isinstance(plan, DslDefinitionSchema)
    assert plan.select == ["name"]


@pytest.mark.parametrize("bad", [["select"], "select", 42, None])
def test_load_plan_rejects_non_dict(bad):
    with pytest.raises(ValueError, match="must be an object"):
        _load_plan(bad)


def test_load_plan_wraps_validation_errors_as_friendly_value_error():
    with pytest.raises(ValueError) as excinfo:
        _load_plan({"select": []})  # neither select nor metrics provided
    assert not isinstance(excinfo.value, ValidationError)
    assert "dsl_definition is invalid" in str(excinfo.value)


# ===========================================================================
# _format_plan_error
# ===========================================================================


def _validation_error(payload) -> ValidationError:
    try:
        DslDefinitionSchema(**payload)
    except ValidationError as exc:
        return exc
    raise AssertionError("expected a validation error")


def test_format_plan_error_select_branch():
    err = _validation_error(
        {"select": [123]}
    )
    msg = _format_plan_error(err)
    assert "'select' must be a list of plain field-name strings" in msg


def test_format_plan_error_group_by_branch():
    err = _validation_error(
        {
            "select": ["created_at"],
            "group_by": [123],
        }
    )
    msg = _format_plan_error(err)
    assert "'group_by' entries must be plain field-name strings" in msg


def test_format_plan_error_generic_fallback():
    err = _validation_error({})
    msg = _format_plan_error(err)
    assert msg.startswith("The dsl_definition is invalid:")
    assert "<plan>" in msg  # empty location rendered as <plan>


# ===========================================================================
# execute_query_tool
# ===========================================================================


def test_execute_query_tool_cache_hit_skips_database(faked_db, fake_redis, monkeypatch):
    fake_redis.hit_value = json.dumps([{"region": "east"}])
    calls = patch_compiler(monkeypatch, rows=[])

    result = execute_query_tool("dst_1", {"select": ["region"]})

    assert result == {"data": [{"region": "east"}], "cached": True}
    assert FakeSession.instances == []  # no database session opened
    assert calls == []


def test_execute_query_tool_cache_miss_compiles_and_stores(
    faked_db, fake_redis, monkeypatch
):
    FakeSession.dataset_to_return = Dataset(user_id=1, name="ds", id="dst_9")
    rows = [{"region": "west"}, {"region": "east"}]
    calls = patch_compiler(monkeypatch, rows=rows)

    result = execute_query_tool("dst_9", {"select": ["region"]})

    assert result == {"data": rows, "cached": False}
    # Compiler received the validated plan plus the dataset id.
    assert len(calls) == 1
    executed_plan, executed_ds_id = calls[0]
    assert isinstance(executed_plan, DslDefinitionSchema)
    assert executed_ds_id == "dst_9"

    # Result stored in redis with the configured expiry.
    assert len(fake_redis.set_calls) == 1
    key, value, ttl = fake_set_call = fake_redis.set_calls[0]
    assert ttl == ex.settings.CACHE_EXPIRE_SECONDS
    assert json.loads(value) == rows
    # Read and write target the same cache key.
    assert fake_redis.get_calls == [key]


def test_execute_query_tool_empty_results_store_empty_list(
    faked_db, fake_redis, monkeypatch
):
    FakeSession.dataset_to_return = Dataset(user_id=1, name="ds", id="dst_1")
    patch_compiler(monkeypatch, rows=[])
    result = execute_query_tool("dst_1", {"select": ["name"]})
    assert result["data"] == []
    assert fake_redis.set_calls[0][1] == "[]"


def test_execute_query_tool_unknown_dataset_raises(faked_db, fake_redis, monkeypatch):
    FakeSession.dataset_to_return = None
    patch_compiler(monkeypatch, rows=[])
    with pytest.raises(ValueError, match="Dataset dst_missing not found"):
        execute_query_tool("dst_missing", {"select": ["name"]})


def test_execute_query_tool_invalid_plan_rejected_before_any_io(
    faked_db, fake_redis, monkeypatch
):
    calls = patch_compiler(monkeypatch, rows=[])
    with pytest.raises(ValueError, match="invalid"):
        execute_query_tool("dst_1", {})  # no select/metrics
    assert calls == []
    assert fake_redis.get_calls == []


# ===========================================================================
# validate_dsl_tool
# ===========================================================================


def test_validate_dsl_tool_success_under_limit(faked_db, monkeypatch):
    FakeSession.dataset_to_return = Dataset(user_id=1, name="ds", id="dst_2")
    patch_compiler(monkeypatch, rows=[{"n": i} for i in range(5)])

    result = validate_dsl_tool("dst_2", {"select": ["name"]})

    assert result["status"] == "validated"
    assert result["total_rows"] == 5
    assert result["truncated"] is False
    assert result["data"] == [{"n": 0}, {"n": 1}, {"n": 2}, {"n": 3}, {"n": 4}]
    assert "5 row(s)" in result["message"]


def test_validate_dsl_tool_truncates_over_twenty_rows(faked_db, monkeypatch):
    FakeSession.dataset_to_return = Dataset(user_id=1, name="ds", id="dst_3")
    patch_compiler(monkeypatch, rows=[{"n": i} for i in range(30)])

    result = validate_dsl_tool("dst_3", {"select": ["name"]})

    assert result["total_rows"] == 30
    assert result["truncated"] is True
    assert len(result["data"]) == 20
    assert result["data"][19] == {"n": 19}


def test_validate_dsl_tool_unknown_dataset_raises(faked_db, monkeypatch):
    FakeSession.dataset_to_return = None
    patch_compiler(monkeypatch, rows=[])
    with pytest.raises(ValueError, match="not found"):
        validate_dsl_tool("dst_x", {"select": ["name"]})


def test_validate_dsl_tool_invalid_plan_raises_before_db(faked_db, monkeypatch):
    calls = patch_compiler(monkeypatch, rows=[])
    with pytest.raises(ValueError):
        validate_dsl_tool("dst_1", {"metrics": "nope"})
    assert calls == []
    assert FakeSession.instances == []


# ===========================================================================
# get_profile_tool / _cache_get_profile
# ===========================================================================


def test_profile_computed_on_miss_then_served_from_cache():
    first = _cache_get_profile("dst_1", None)
    assert first == {"columns": [], "rows": []}
    assert len(FakeProfiler.instances) == 1
    assert FakeProfiler.instances[0].dataset_id == "dst_1"
    assert FakeProfiler.instances[0].dsl_definition is None

    second = _cache_get_profile("dst_1", None)
    assert second == first
    assert len(FakeProfiler.instances) == 1  # no recompute


def test_profile_cache_separates_datasets_and_plans():
    _cache_get_profile("dst_1", None)
    _cache_get_profile("dst_2", None)
    _cache_get_profile("dst_1", {"select": ["amount"]})
    assert len(FakeProfiler.instances) == 3


def test_profile_validates_and_cleans_dsl_definition():
    # limit=None must be stripped by exclude_none; default empty lists stay.
    _cache_get_profile("dst_1", {"select": ["amount"], "limit": None})
    profiler = FakeProfiler.instances[0]
    assert "limit" not in profiler.dsl_definition
    assert profiler.dsl_definition == {
        "select": ["amount"],
        "filters": [],
        "group_by": [],
        "metrics": [],
        "sorts": [],
    }


def test_profile_expired_entry_recomputed(monkeypatch):
    _cache_get_profile("dst_1", None)
    assert len(FakeProfiler.instances) == 1

    # Age every cache entry past the TTL.
    stale = {k: (time.time() - (_PROFILE_TTL + 10), v) for k, (_, v) in ex._profile_cache.items()}
    monkeypatch.setattr(ex, "_profile_cache", stale)

    _cache_get_profile("dst_1", None)
    assert len(FakeProfiler.instances) == 2
    # Stale entry was evicted and replaced with a fresh timestamp.
    assert all(time.time() - ts < _PROFILE_TTL for ts, _ in ex._profile_cache.values())


def test_profile_fresh_entry_within_ttl_not_recomputed():
    _cache_get_profile("dst_1", None)
    # Simulate an entry that is 10 seconds old (well within TTL).
    fresh = {k: (time.time() - 10, {"cached_marker": True}) for k, (_, _) in ex._profile_cache.items()}
    ex._profile_cache.update(fresh)
    result = _cache_get_profile("dst_1", None)
    assert result == {"cached_marker": True}
    assert len(FakeProfiler.instances) == 1


def test_get_profile_tool_validates_dsl_and_delegates():
    report = get_profile_tool("dst_5", {"select": ["amount"], "limit": None})
    assert report == {"columns": [], "rows": []}
    profiler = FakeProfiler.instances[0]
    assert profiler.dsl_definition["select"] == ["amount"]
    assert "limit" not in profiler.dsl_definition


def test_get_profile_tool_rejects_invalid_dsl():
    with pytest.raises(ValidationError):
        get_profile_tool("dst_5", {"select": []})


# ===========================================================================
# Redis-independent sanity: gen_cache_key integration shape
# ===========================================================================


def test_execute_query_tool_cache_key_depends_on_dataset_and_plan(
    faked_db, fake_redis, monkeypatch
):
    FakeSession.dataset_to_return = Dataset(user_id=1, name="ds", id="dst_1")
    patch_compiler(monkeypatch, rows=[{"a": 1}])

    r1 = execute_query_tool("dst_1", {"select": ["a"]})
    key_first = fake_redis.set_calls[-1][0]

    # Same dataset+plan (different key order) -> identical cache key.
    fake_redis.hit_value = json.dumps([{"a": 1}])
    r2 = execute_query_tool("dst_1", {"select": ["a"]})
    assert r2 == {"data": [{"a": 1}], "cached": True}
    assert fake_redis.get_calls[-1] == key_first

    # Different plan -> different key.
    FakeSession.dataset_to_return = Dataset(user_id=1, name="ds", id="dst_1")
    fake_redis.hit_value = None
    execute_query_tool("dst_1", {"select": ["b"]})
    key_second = fake_redis.set_calls[-1][0]
    assert key_second != key_first