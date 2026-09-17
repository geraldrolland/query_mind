"""Shared fixtures for the auth endpoint test package.

Builds a minimal FastAPI app containing only the auth router, overrides the
DB session dependency with a configurable fake, replaces both Redis clients
on the auth module, and injects ``request.state.auth_user`` via middleware —
no real database, Redis, or email traffic.
"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import get_session
from app.models.user import User
from app.routers import auth as auth_mod
from app.routers.auth import router as auth_router


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeResult:
    def __init__(self, items):
        self._items = list(items)

    def first(self):
        return self._items[0] if self._items else None


class FakeDBSession:
    """sqlmodel.Session stand-in driven by per-test configuration.

    - ``query_results``: FIFO of lists returned by successive exec(...).first()
    - ``get_results``:   pk -> object returned by session.get()
    """

    def __init__(self):
        self.query_results = []
        self.get_results = {}
        self.added = []
        self.commits = 0

    def exec(self, query):
        if self.query_results:
            return FakeResult(self.query_results.pop(0))
        return FakeResult([])

    def get(self, model, pk):
        return self.get_results.get(pk)

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.commits += 1

    def refresh(self, obj):
        pass


class FakeRedis:
    """Dict-backed redis stand-in recording every operation."""

    def __init__(self, store=None):
        self.store = dict(store or {})
        self.set_calls = []
        self.deleted = []

    def set(self, key, value, ex=None):
        self.store[key] = str(value)
        self.set_calls.append((key, value, ex))

    def get(self, key):
        value = self.store.get(key)
        return value

    def delete(self, *keys):
        for key in keys:
            self.deleted.append(key)
            self.store.pop(key, None)


def _build_user(
    uid=1,
    email="u@x.com",
    verified=True,
    password_hash=None,
    provider="email",
) -> User:
    return User(
        id=uid,
        email=email,
        hashed_password=password_hash,
        auth_provider=provider,
        is_email_verified=verified,
    )


@pytest.fixture
def make_user():
    """Factory fixture: make_user(uid=1, email=..., verified=True, ...)."""
    return _build_user


STRONG_PASSWORD = "Str0ng!pass"


@pytest.fixture
def strong_password():
    return STRONG_PASSWORD


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_db():
    return FakeDBSession()


@pytest.fixture
def fake_redis():
    return FakeRedis()


@pytest.fixture
def fake_refresh_redis():
    return FakeRedis()


@pytest.fixture
def auth_ns(monkeypatch, fake_db, fake_redis, fake_refresh_redis):
    """Bundle: TestClient + fakes. Patch module-level clients on auth."""
    monkeypatch.setattr(auth_mod, "redis_client", fake_redis)
    monkeypatch.setattr(auth_mod, "refresh_redis", fake_refresh_redis)

    app = FastAPI()
    app.include_router(auth_router)

    state_box = {"auth_user": {"id": 7, "email": "me@x.com"}}

    @app.middleware("http")
    async def inject_auth_state(request, call_next):
        # Stands in for AuthenticationMiddleware in endpoint tests.
        request.state.auth_user = state_box["auth_user"]
        return await call_next(request)

    def override_session():
        yield fake_db

    app.dependency_overrides[get_session] = override_session

    return SimpleNamespace(
        client=TestClient(app),
        db=fake_db,
        redis=fake_redis,
        refresh_redis=fake_refresh_redis,
        state=state_box,
        settings=auth_mod.settings,
    )


@pytest.fixture
def make_session_cookie():
    """Returns (signed_cookie_value, refresh_jti)."""

    def _make(user_id=1, email="u@x.com"):
        tokens = auth_mod.make_tokens(user_id, email, "pytest-agent")
        signed = auth_mod.sign_cookie(
            {"access_token": tokens["access"], "refresh_token": tokens["refresh"]}
        )
        return signed, tokens["jti"]

    return _make


@pytest.fixture
def recorder():
    """Tiny call recorder for patched helpers."""

    class Recorder:
        def __init__(self):
            self.calls = []

        def __call__(self, *args, **kwargs):
            self.calls.append((args, kwargs))

        @property
        def count(self):
            return len(self.calls)

    return Recorder()


@pytest.fixture
def patch_send_verification(monkeypatch, recorder):
    """Replace _send_verification_link on the auth module."""

    def _patch():
        monkeypatch.setattr(auth_mod, "_send_verification_link", recorder)
        return recorder

    return _patch


STRONG_PASSWORD = "Str0ng!pass"
