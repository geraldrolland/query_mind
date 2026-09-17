"""Unit tests for the backend middleware package (middleware/__init__.py).

Covers:
    - RequestLoggingMiddleware   (request/response logging)
    - _cors_headers              (CORS echo logic)
    - is_public                  (public path rules)
    - AuthenticationMiddleware   (auth_token cookie -> 401s / state.auth_user)
    - _agent_matches             (user agent comparison)
    - _cache_key                 (redis cache key derivation)
    - invalidate_user_cache      (scan+delete flow)
    - CacheMiddleware            (GET caching on /api/v1/datasets)

pytest does not auto-collect ``__init__.py``, so run explicitly:

    python -m pytest tests/test_middleware/__init__.py -v
"""

import asyncio
import json
import logging
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

import middleware as mw
from app.auth import utils as auth_utils
from middleware import (
    CACHE_TTL_SECONDS,
    AuthenticationMiddleware,
    CacheMiddleware,
    RequestLoggingMiddleware,
    _agent_matches,
    _cache_key,
    _cors_headers,
    invalidate_user_cache,
    is_public,
)

# ===========================================================================
# Helpers
# ===========================================================================

SENTINEL = JSONResponse({"passthrough": True})


def make_request(
    method: str = "GET",
    path: str = "/",
    query: str = "",
    headers: dict | None = None,
) -> Request:
    """Build a real Starlette Request from an ASGI scope (no network)."""
    raw_headers = [
        (name.lower().encode("latin-1"), value.encode("latin-1"))
        for name, value in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "headers": raw_headers,
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive=receive)


def signed_auth_cookie(
    sub: str = "7",
    email: str = "u@x.com",
    token_user_agent: str = "",
) -> str:
    access = auth_utils.generate_access_token(
        {"sub": sub, "email": email, "user_agent": token_user_agent}
    )
    return auth_utils.sign_cookie({"access_token": access})


def stream_response(body: bytes, status_code: int = 200, media_type: str | None = None) -> Response:
    """Starlette Response carrying an async body_iterator, like streaming apps."""

    async def gen():
        yield body

    resp = Response(content=body, status_code=status_code, media_type=media_type)
    resp.body_iterator = gen()
    return resp


class FakeRedis:
    """Records calls; behaves like an empty cache."""

    def __init__(self):
        self.get_calls = []
        self.set_calls = []
        self.deleted = []

    def get(self, key):
        self.get_calls.append(key)
        return None

    def set(self, key, value, ex=None):
        self.set_calls.append((key, value, ex))

    def delete(self, *keys):
        self.deleted.extend(keys)


# ===========================================================================
# _cors_headers
# ===========================================================================


def test_cors_headers_allowed_origin(monkeypatch):
    monkeypatch.setattr(
        mw.settings, "CORS_ORIGINS", ["http://allowed.example"]
    )
    req = make_request(headers={"origin": "http://allowed.example"})
    assert _cors_headers(req) == {
        "Access-Control-Allow-Origin": "http://allowed.example",
        "Access-Control-Allow-Credentials": "true",
        "Vary": "Origin",
    }


def test_cors_headers_disallowed_origin_empty(monkeypatch):
    monkeypatch.setattr(
        mw.settings, "CORS_ORIGINS", ["http://allowed.example"]
    )
    req = make_request(headers={"origin": "http://evil.example"})
    assert _cors_headers(req) == {}


def test_cors_headers_no_origin_header(monkeypatch):
    monkeypatch.setattr(
        mw.settings, "CORS_ORIGINS", ["http://allowed.example"]
    )
    assert _cors_headers(make_request()) == {}


# ===========================================================================
# is_public
# ===========================================================================


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/auth/register",
        "/api/v1/auth/login",
        # "/api/v1/auth/google-auth",   # disabled — Google OAuth removed
        # "/api/v1/auth/google/url",     # disabled — Google OAuth removed
        "/api/v1/auth/refresh-token",
        # "/api/v1/auth/session",        # disabled — OAuth session exchange removed
        "/health",
        "/docs",
        "/openapi.json",
        "/redoc",
    ],
)
def test_is_public_exact_paths(path):
    assert is_public(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/auth/email/change",
        "/api/v1/auth/verify-email",
        "/api/v1/auth/reset-password/confirm",
        "/api/v1/auth/verify-otp",
        "/api/v1/auth/resend-otp",
    ],
)
def test_is_public_auth_subroutes(path):
    assert is_public(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/auth/logout",
        "/api/v1/auth/something-else",
        "/api/v1/datasets",
        "/api/v1/authenticate",
        "/",
        "/api/v2/auth/login",
    ],
)
def test_is_public_private_paths(path):
    assert is_public(path) is False


# ===========================================================================
# RequestLoggingMiddleware
# ===========================================================================


def test_logging_middleware_logs_req_and_res_and_passes_through(caplog):
    mw_inst = RequestLoggingMiddleware(app=None)

    async def nxt(request):
        return SENTINEL

    req = make_request(method="GET", path="/api/v1/datasets")
    with caplog.at_level(logging.INFO, logger="middleware"):
        result = asyncio.run(mw_inst.dispatch(req, nxt))

    assert result is SENTINEL
    messages = [record.getMessage() for record in caplog.records]
    assert any(m.startswith("REQ GET /api/v1/datasets") for m in messages)
    assert any(
        m.startswith("RES GET /api/v1/datasets -> 200") and "ms" in m
        for m in messages
    )


def test_logging_middleware_includes_query_string(caplog):
    mw_inst = RequestLoggingMiddleware(app=None)
    req = make_request(method="POST", path="/api/v1/upload", query="a=1&b=2")

    async def nxt(request):
        return SENTINEL

    with caplog.at_level(logging.INFO, logger="middleware"):
        asyncio.run(mw_inst.dispatch(req, nxt))

    messages = [record.getMessage() for record in caplog.records]
    assert any("REQ POST /api/v1/upload?a=1&b=2" in m for m in messages)


# ===========================================================================
# AuthenticationMiddleware — pass-through branches
# ===========================================================================


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("OPTIONS", "/api/v1/datasets"),  # CORS preflight always passes
        ("GET", "/api/v1/auth/login"),  # public path
        ("GET", "/some/non-api/page"),  # outside /api/v1
        ("GET", "/health"),  # public even though it would match the guard
    ],
)
def test_auth_middleware_passthrough(method, path):
    mw_inst = AuthenticationMiddleware(app=None)
    nxt = AsyncMock(return_value=SENTINEL)
    req = make_request(method=method, path=path)  # deliberately no cookies
    result = asyncio.run(mw_inst.dispatch(req, nxt))
    assert result is SENTINEL
    assert nxt.await_count == 1


# ===========================================================================
# AuthenticationMiddleware — rejection branches
# ===========================================================================


def test_auth_middleware_missing_cookie_401():
    mw_inst = AuthenticationMiddleware(app=None)
    nxt = AsyncMock(return_value=SENTINEL)
    req = make_request(path="/api/v1/datasets")
    result = asyncio.run(mw_inst.dispatch(req, nxt))
    assert result.status_code == 401
    assert json.loads(result.body) == {"detail": "Not authenticated"}
    assert nxt.await_count == 0


def test_auth_middleware_invalid_signed_cookie_401():
    mw_inst = AuthenticationMiddleware(app=None)
    nxt = AsyncMock(return_value=SENTINEL)
    req = make_request(
        path="/api/v1/datasets",
        headers={"cookie": "auth_token=tampered-or-garbage"},
    )
    result = asyncio.run(mw_inst.dispatch(req, nxt))
    assert result.status_code == 401
    assert json.loads(result.body) == {"detail": "Invalid session"}
    assert nxt.await_count == 0


def test_auth_middleware_undecodable_jwt_401():
    mw_inst = AuthenticationMiddleware(app=None)
    nxt = AsyncMock(return_value=SENTINEL)
    # Validly signed envelope, but the inner access token is not a JWT.
    cookie = auth_utils.sign_cookie({"access_token": "not-a-jwt"})
    req = make_request(
        path="/api/v1/datasets",
        headers={"cookie": f"auth_token={cookie}"},
    )
    result = asyncio.run(mw_inst.dispatch(req, nxt))
    assert result.status_code == 401
    assert json.loads(result.body) == {"detail": "Access token expired"}
    assert nxt.await_count == 0


def test_auth_middleware_agent_mismatch_401():
    mw_inst = AuthenticationMiddleware(app=None)
    nxt = AsyncMock(return_value=SENTINEL)
    cookie = signed_auth_cookie(token_user_agent="Chrome-on-Linux")
    req = make_request(
        path="/api/v1/datasets",
        headers={"cookie": f"auth_token={cookie}", "user-agent": "Firefox"},
    )
    result = asyncio.run(mw_inst.dispatch(req, nxt))
    assert result.status_code == 401
    assert json.loads(result.body) == {"detail": "Session agent mismatch"}
    assert nxt.await_count == 0


def test_auth_middleware_rejections_include_cors_headers(monkeypatch):
    monkeypatch.setattr(
        mw.settings, "CORS_ORIGINS", ["http://frontend.example"]
    )
    mw_inst = AuthenticationMiddleware(app=None)
    nxt = AsyncMock(return_value=SENTINEL)
    req = make_request(
        path="/api/v1/datasets",
        headers={"origin": "http://frontend.example"},  # no cookie
    )
    result = asyncio.run(mw_inst.dispatch(req, nxt))
    assert result.status_code == 401
    assert result.headers["access-control-allow-origin"] == "http://frontend.example"
    assert result.headers["access-control-allow-credentials"] == "true"


# ===========================================================================
# AuthenticationMiddleware — happy path
# ===========================================================================


def test_auth_middleware_success_sets_state_auth_user():
    mw_inst = AuthenticationMiddleware(app=None)
    captured = {}

    async def nxt(request):
        captured["state"] = getattr(request.state, "auth_user", None)
        return SENTINEL

    cookie = signed_auth_cookie(sub="42", email="u@x.com", token_user_agent="UA-1")
    req = make_request(
        path="/api/v1/datasets",
        headers={"cookie": f"auth_token={cookie}", "user-agent": "UA-1"},
    )
    result = asyncio.run(mw_inst.dispatch(req, nxt))
    assert result is SENTINEL
    assert captured["state"] == {
        "id": 42,
        "email": "u@x.com",
        "user_agent": "UA-1",
    }


def test_auth_middleware_success_when_token_has_no_user_agent():
    mw_inst = AuthenticationMiddleware(app=None)
    nxt = AsyncMock(return_value=SENTINEL)
    # Token carries empty user_agent -> agent check skipped entirely.
    cookie = signed_auth_cookie(token_user_agent="")
    req = make_request(
        path="/api/v1/datasets",
        headers={"cookie": f"auth_token={cookie}", "user-agent": "Whatever"},
    )
    result = asyncio.run(mw_inst.dispatch(req, nxt))
    assert result is SENTINEL
    assert nxt.await_count == 1


# ===========================================================================
# _agent_matches
# ===========================================================================


def test_agent_matches_equal_strings():
    assert _agent_matches("Mozilla/5.0", "Mozilla/5.0") is True


def test_agent_matches_different_strings():
    assert _agent_matches("Mozilla/5.0", "curl/8.0") is False


# ===========================================================================
# _cache_key
# ===========================================================================


def test_cache_key_deterministic_and_prefixed():
    k1 = _cache_key(9, "GET", "/api/v1/datasets", "page=2", b'{"x":1}')
    k2 = _cache_key(9, "GET", "/api/v1/datasets", "page=2", b'{"x":1}')
    assert k1 == k2
    assert k1.startswith(f"cache:v1:9:")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda: _cache_key(10, "GET", "/p", "", b""),  # different user
        lambda: _cache_key(9, "POST", "/p", "", b""),  # different method
        lambda: _cache_key(9, "GET", "/other", "", b""),  # different path
        lambda: _cache_key(9, "GET", "/p", "q=1", b""),  # different query
        lambda: _cache_key(9, "GET", "/p", "", b"x"),  # different body
    ],
)
def test_cache_key_varies_with_every_component(mutate):
    base = _cache_key(9, "GET", "/p", "", b"")
    assert mutate() != base


# ===========================================================================
# invalidate_user_cache
# ===========================================================================


class PagedScanRedis(FakeRedis):
    """scan() replays queued (cursor, keys) pages."""

    def __init__(self, pages):
        super().__init__()
        self.pages = list(pages)
        self.matches = []

    def scan(self, cursor=0, match=None, count=None):
        self.matches.append(match)
        return self.pages.pop(0)


def test_invalidate_user_cache_scans_and_deletes_all_pages(monkeypatch):
    fake = PagedScanRedis(pages=[(1, ["k1", "k2"]), (0, ["k3"])])
    monkeypatch.setattr(mw, "redis_client", fake)
    invalidate_user_cache(5)
    assert fake.matches == ["cache:v1:5:*"] * 2
    assert sorted(fake.deleted) == ["k1", "k2", "k3"]


def test_invalidate_user_cache_swallows_redis_errors(monkeypatch):
    class ExplodingRedis:
        def scan(self, **kwargs):
            raise RuntimeError("redis down")

    monkeypatch.setattr(mw, "redis_client", ExplodingRedis())
    invalidate_user_cache(5)  # must not raise


# ===========================================================================
# CacheMiddleware — pass-through branches
# ===========================================================================


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/auth/login"),  # not a SAFE_CACHE_PATHS prefix
        ("POST", "/api/v1/datasets"),  # only GET cached
        ("DELETE", "/api/v1/datasets"),
    ],
)
def test_cache_middleware_non_cacheable_requests_pass_through(method, path):
    mw_inst = CacheMiddleware(app=None)
    nxt = AsyncMock(return_value=SENTINEL)
    req = make_request(method=method, path=path)
    result = asyncio.run(mw_inst.dispatch(req, nxt))
    assert result is SENTINEL
    assert nxt.await_count == 1


def test_cache_middleware_without_auth_user_passes_through():
    mw_inst = CacheMiddleware(app=None)
    nxt = AsyncMock(return_value=SENTINEL)
    req = make_request(method="GET", path="/api/v1/datasets")  # no state.auth_user
    result = asyncio.run(mw_inst.dispatch(req, nxt))
    assert result is SENTINEL
    assert nxt.await_count == 1


# ===========================================================================
# CacheMiddleware — cache hit paths
# ===========================================================================


def _get_request_for_user(user_id: int) -> Request:
    req = make_request(method="GET", path="/api/v1/datasets", query="page=1")
    req.state.auth_user = {"id": user_id}
    return req


def test_cache_middleware_hit_returns_cached_response_without_calling_next(monkeypatch):
    mw_inst = CacheMiddleware(app=None)
    nxt = AsyncMock(return_value=SENTINEL)

    class HitRedis(FakeRedis):
        def get(self, key):
            self.get_calls.append(key)
            return json.dumps(
                {
                    "status": 201,
                    "media_type": "application/json",
                    "body": {"cached": True},
                }
            )

    fake = HitRedis()
    monkeypatch.setattr(mw, "redis_client", fake)
    result = asyncio.run(
        mw_inst.dispatch(_get_request_for_user(9), nxt)
    )
    assert result.status_code == 201
    assert json.loads(result.body) == {"cached": True}
    assert nxt.await_count == 0
    assert len(fake.get_calls) == 1


def test_cache_middleware_corrupt_entry_falls_through_to_backend(monkeypatch):
    mw_inst = CacheMiddleware(app=None)
    nxt = AsyncMock(return_value=SENTINEL)

    class CorruptRedis(FakeRedis):
        def get(self, key):
            self.get_calls.append(key)
            return b"{not-json-at-all"

    monkeypatch.setattr(mw, "redis_client", CorruptRedis())
    result = asyncio.run(
        mw_inst.dispatch(_get_request_for_user(9), nxt)
    )
    assert result is SENTINEL
    assert nxt.await_count == 1


def test_cache_middleware_redis_read_failure_falls_through(monkeypatch):
    mw_inst = CacheMiddleware(app=None)
    nxt = AsyncMock(return_value=SENTINEL)

    class DownRedis:
        def get(self, key):
            raise ConnectionError("redis down")

    monkeypatch.setattr(mw, "redis_client", DownRedis())
    result = asyncio.run(
        mw_inst.dispatch(_get_request_for_user(9), nxt)
    )
    assert result is SENTINEL
    assert nxt.await_count == 1


# ===========================================================================
# CacheMiddleware — backend response handling / cache writes
# ===========================================================================


def test_cache_middleware_caches_200_json_response(monkeypatch):
    mw_inst = CacheMiddleware(app=None)
    fake = FakeRedis()

    async def nxt(request):
        return stream_response(b'{"ok": true}', 200, "application/json")

    monkeypatch.setattr(mw, "redis_client", fake)
    result = asyncio.run(
        mw_inst.dispatch(_get_request_for_user(9), nxt)
    )

    # Rebuilt response carries the original body.
    assert isinstance(result, Response)
    assert result.body == b'{"ok": true}'
    assert result.status_code == 200

    # Exactly one cache write with TTL and correct key/payload shape.
    assert len(fake.set_calls) == 1
    key, value, ttl = fake.set_calls[0]
    assert key.startswith("cache:v1:9:")
    assert ttl == CACHE_TTL_SECONDS
    stored = json.loads(value)
    assert stored == {
        "status": 200,
        "media_type": "application/json",
        "body": {"ok": True},
    }


def test_cache_middleware_does_not_cache_non_200(monkeypatch):
    mw_inst = CacheMiddleware(app=None)
    fake = FakeRedis()

    async def nxt(request):
        return Response(content=b"nope", status_code=404)

    monkeypatch.setattr(mw, "redis_client", fake)
    result = asyncio.run(
        mw_inst.dispatch(_get_request_for_user(9), nxt)
    )
    assert result.status_code == 404
    assert fake.set_calls == []


def test_cache_middleware_does_not_cache_non_json_content_type(monkeypatch):
    mw_inst = CacheMiddleware(app=None)
    fake = FakeRedis()

    async def nxt(request):
        return stream_response(b"<h1>hi</h1>", 200, "text/html")

    monkeypatch.setattr(mw, "redis_client", fake)
    result = asyncio.run(
        mw_inst.dispatch(_get_request_for_user(9), nxt)
    )
    assert result.body == b"<h1>hi</h1>"
    assert fake.set_calls == []


def test_cache_middleware_does_not_cache_empty_json_body(monkeypatch):
    mw_inst = CacheMiddleware(app=None)
    fake = FakeRedis()

    async def nxt(request):
        return stream_response(b"", 200, "application/json")

    monkeypatch.setattr(mw, "redis_client", fake)
    result = asyncio.run(
        mw_inst.dispatch(_get_request_for_user(9), nxt)
    )
    assert fake.set_calls == []
