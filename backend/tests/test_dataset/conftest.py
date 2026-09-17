"""Shared fixtures for the dataset endpoint test package."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import get_session
from app.models.dataset import Dataset
from app.models.datasetrow import DatasetRow
from app.models.message import Message
from app.routers import datasets as ds_mod
from app.routers.datasets import router as datasets_router


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeResult:
    """Mimics sqlalchemy Result for first()/all()/one() consumers."""

    def __init__(self, items=None, scalar=None):
        self._items = list(items or [])
        self._scalar = scalar

    def first(self):
        return self._items[0] if self._items else None

    def all(self):
        return self._items

    def one(self):
        return self._scalar


class FakeDBSession:
    """sqlmodel.Session stand-in: queue exec() results, record mutations.

    - ``query_results``: FIFO of FakeResults consumed by successive exec()s.
      Use ``queue_result(items=[...])`` or ``queue_result(scalar=5)``.
    - ``get_results``:   pk -> object returned by session.get().
    """

    def __init__(self):
        self.query_results = []
        self.get_results = {}
        self.added = []
        self.add_all_calls = []
        self.deleted_objects = []
        self.flushes = 0
        self.commits = 0

    def queue_result(self, items=None, scalar=None):
        self.query_results.append(FakeResult(items=items, scalar=scalar))

    def exec(self, query):
        if self.query_results:
            return self.query_results.pop(0)
        return FakeResult()

    def get(self, model, pk):
        return self.get_results.get(pk)

    def add(self, obj):
        self.added.append(obj)

    def add_all(self, objs):
        self.add_all_calls.append(list(objs))

    def delete(self, obj):
        self.deleted_objects.append(obj)

    def flush(self):
        self.flushes += 1

    def commit(self):
        self.commits += 1

    def refresh(self, obj):
        pass


# ---------------------------------------------------------------------------
# Object factories
# ---------------------------------------------------------------------------


def _build_dataset(user_id=7, **overrides) -> Dataset:
    defaults = dict(
        id="dst_1",
        name="Sales",
        description=None,
        populated=True,
        total_rows=3,
        total_size_bytes=120,
        dataset_schema={"name": {"type": "string"}},
    )
    defaults.update(overrides)
    return Dataset(user_id=user_id, **defaults)


def _build_row(dataset_id="dst_1", idx=0, data=None) -> DatasetRow:
    return DatasetRow(
        id=f"drow_{idx}", dataset_id=dataset_id, data=data or {"n": idx}
    )


def _build_message(dataset_id="dst_1", idx=0, **overrides) -> Message:
    defaults = dict(
        id=f"msg_{idx}",
        dataset_id=dataset_id,
        content="hello there",
        role="user",
        type="text",
        chart_type="none",
        is_error=False,
    )
    defaults.update(overrides)
    return Message(**defaults)


@pytest.fixture
def make_dataset():
    return _build_dataset


@pytest.fixture
def make_row():
    return _build_row


@pytest.fixture
def make_message():
    return _build_message


# ---------------------------------------------------------------------------
# App / client fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_db():
    return FakeDBSession()


@pytest.fixture
def cache_recorder(monkeypatch):
    """Patches middleware.invalidate_user_cache at its source module.

    The upload/delete handlers do a lazy ``from middleware import
    invalidate_user_cache`` inside the function body, so patching must
    happen on the middleware module itself.
    """
    calls = []
    monkeypatch.setattr("middleware.invalidate_user_cache", lambda uid: calls.append(uid))
    return calls


@pytest.fixture
def ds_ns(monkeypatch, fake_db):
    """Bundle: TestClient + fakes + mutable auth state + settings handle."""
    app = FastAPI()
    app.include_router(datasets_router)

    state_box = {"auth_user": {"id": 7}}

    @app.middleware("http")
    async def inject_auth_state(request, call_next):
        request.state.auth_user = state_box["auth_user"]
        return await call_next(request)

    def override_session():
        yield fake_db

    app.dependency_overrides[get_session] = override_session

    return SimpleNamespace(
        client=TestClient(app),
        db=fake_db,
        state=state_box,
        settings=ds_mod.settings,
    )


CSV_OK = b"name,age\nAlice,30\nBob,25\nAlice,30\n"  # one exact duplicate row
