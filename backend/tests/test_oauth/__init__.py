"""Unit tests for app/auth/oauth.py (Google OAuth2 helpers).

Covers:
    - build_google_auth_url   (URL assembly from settings)
    - exchange_code_for_user  (token exchange + userinfo fetch, error paths)
    - check_user_agent_match  (None handling)
    - hmac_compare            (constant-time string comparison)

Network calls are faked with ``httpx.MockTransport``; the AsyncClient
constructor is patched so no real HTTP traffic happens.

pytest does not auto-collect ``__init__.py``, so run explicitly:

    python -m pytest tests/test_oauth/__init__.py -v
"""

import asyncio
import urllib.parse

import httpx
import pytest

from app.auth import oauth

# ===========================================================================
# Helpers
# ===========================================================================


def patch_settings(monkeypatch, **overrides):
    """Point the oauth module's settings at deterministic test values."""
    defaults = {
        "GOOGLE_CLIENT_ID": "client-id-123",
        "GOOGLE_CLIENT_SECRET": "client-secret-456",
        "GOOGLE_REDIRECT_URI": "http://localhost:8000/api/v1/auth/google-auth",
        "GOOGLE_AUTH_URL": "https://accounts.google.com/o/oauth2/v2/auth",
        "GOOGLE_TOKEN_URL": "https://oauth2.googleapis.com/token",
        "GOOGLE_USERINFO_URL": "https://www.googleapis.com/oauth2/v2/userinfo",
    }
    defaults.update(overrides)
    for name, value in defaults.items():
        monkeypatch.setattr(oauth.settings, name, value)


def patch_async_client(monkeypatch, handler):
    """Route every httpx.AsyncClient created by oauth through MockTransport."""
    real_client = httpx.AsyncClient  # captured before we shadow the attribute

    def factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        kwargs.pop("transport", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(oauth.httpx, "AsyncClient", factory)


# ===========================================================================
# build_google_auth_url
# ===========================================================================


def test_build_google_auth_url_base_and_params(monkeypatch):
    patch_settings(monkeypatch)
    url = oauth.build_google_auth_url("state-abc")

    parsed = urllib.parse.urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "accounts.google.com"
    assert parsed.path == "/o/oauth2/v2/auth"

    params = urllib.parse.parse_qs(parsed.query)
    assert params["client_id"] == ["client-id-123"]
    assert params["redirect_uri"] == [
        "http://localhost:8000/api/v1/auth/google-auth"
    ]
    assert params["response_type"] == ["code"]
    assert params["prompt"] == ["select_account"]
    assert params["access_type"] == ["offline"]
    assert params["state"] == ["state-abc"]


def test_build_google_auth_url_scope_requests_email_and_profile(monkeypatch):
    patch_settings(monkeypatch)
    url = oauth.build_google_auth_url("s")
    scope = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["scope"][0]
    assert "https://www.googleapis.com/auth/userinfo.email" in scope
    assert "https://www.googleapis.com/auth/userinfo.profile" in scope


def test_build_google_auth_url_state_is_passed_through_verbatim(monkeypatch):
    patch_settings(monkeypatch)
    tricky = "a b&c=d/e?f"
    url = oauth.build_google_auth_url(tricky)
    state = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["state"][0]
    assert state == tricky


# ===========================================================================
# exchange_code_for_user
# ===========================================================================


def test_exchange_code_for_user_success(monkeypatch):
    patch_settings(monkeypatch)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.host + request.url.path] = {
            "method": request.method,
            "headers": dict(request.headers),
            "body": request.content.decode(),
        }
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "tok-1"})
        return httpx.Response(
            200,
            json={"sub": "g-99", "email": "g@gmail.com", "name": "G User"},
        )

    patch_async_client(monkeypatch, handler)
    profile = asyncio.run(oauth.exchange_code_for_user("auth-code-xyz"))

    assert profile == {"sub": "g-99", "email": "g@gmail.com", "name": "G User"}

    # Token exchange POST carries the full authorization-code grant payload.
    token_call = seen["oauth2.googleapis.com/token"]
    assert token_call["method"] == "POST"
    body = urllib.parse.parse_qs(token_call["body"])
    assert body["code"] == ["auth-code-xyz"]
    assert body["client_id"] == ["client-id-123"]
    assert body["client_secret"] == ["client-secret-456"]
    assert body["grant_type"] == ["authorization_code"]
    assert body["redirect_uri"] == [
        "http://localhost:8000/api/v1/auth/google-auth"
    ]

    # Userinfo GET uses the bearer token returned by the token endpoint.
    user_call = seen["www.googleapis.com/oauth2/v2/userinfo"]
    assert user_call["method"] == "GET"
    assert user_call["headers"]["authorization"] == "Bearer tok-1"


def test_exchange_code_for_user_returns_none_on_token_failure(monkeypatch):
    patch_settings(monkeypatch)
    userinfo_called = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(
                400, json={"error": "invalid_grant", "error_description": "bad code"}
            )
        userinfo_called.append(True)
        return httpx.Response(200, json={})

    patch_async_client(monkeypatch, handler)
    result = asyncio.run(oauth.exchange_code_for_user("bad-code"))
    assert result is None
    assert userinfo_called == []  # short-circuits before fetching the profile


def test_exchange_code_for_user_returns_none_on_userinfo_failure(monkeypatch):
    patch_settings(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "tok-2"})
        return httpx.Response(401, json={"error": "unauthorized"})

    patch_async_client(monkeypatch, handler)
    result = asyncio.run(oauth.exchange_code_for_user("some-code"))
    assert result is None


# ===========================================================================
# check_user_agent_match
# ===========================================================================


def test_check_user_agent_match_none_token_agent_is_rejected():
    assert oauth.check_user_agent_match(None, "anything") is False
    assert oauth.check_user_agent_match(None, None) is False


def test_check_user_agent_match_equal_agents():
    assert oauth.check_user_agent_match("Mozilla/5.0", "Mozilla/5.0") is True


def test_check_user_agent_match_different_agents():
    assert oauth.check_user_agent_match("Mozilla/5.0", "curl/8") is False


# ===========================================================================
# hmac_compare
# ===========================================================================


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("same", "same", True),
        ("", "", True),
        ("left", "right", False),
        ("short", "shorter", False),
    ],
)
def test_hmac_compare(a, b, expected):
    assert oauth.hmac_compare(a, b) is expected
