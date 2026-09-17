"""Shared fixtures for the review endpoint test package."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import get_session
from app.routers import review as review_mod
from app.routers.review import router as review_router


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeDBSession:
    """sqlmodel.Session stand-in: records mutations."""

    def __init__(self):
        self.added = []
        self.commits = 0

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.commits += 1

    def refresh(self, obj):
        pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_db():
    return FakeDBSession()


@pytest.fixture
def review_ns(monkeypatch, fake_db):
    """Bundle: TestClient + fakes. Patches module-level send_email."""
    monkeypatch.setattr(review_mod, "send_email", lambda *a, **kw: True)

    app = FastAPI()
    app.include_router(review_router)

    state_box = {"auth_user": {"id": 7, "email": "reviewer@x.com"}}

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
        settings=review_mod.settings,
    )
