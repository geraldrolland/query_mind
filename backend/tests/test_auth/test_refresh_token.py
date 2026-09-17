"""POST /api/v1/auth/refresh-token."""

from app.auth.utils import decode_token


def test_refresh_without_cookie_rejected(auth_ns):
    resp = auth_ns.client.post("/api/v1/auth/refresh-token")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "not authenticated"


def test_refresh_tampered_cookie_rejected(auth_ns):
    auth_ns.client.cookies.set("auth_token", "tampered-or-garbage")
    resp = auth_ns.client.post("/api/v1/auth/refresh-token")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid session"


def test_refresh_undecodable_inner_token_rejected(auth_ns, make_session_cookie):
    from app.routers import auth as auth_mod

    signed, _ = make_session_cookie()
    # Re-sign an envelope whose refresh_token is not a JWT.
    broken = auth_mod.sign_cookie({"refresh_token": "not-a-jwt"})
    assert signed != broken
    auth_ns.client.cookies.set("auth_token", broken)

    resp = auth_ns.client.post("/api/v1/auth/refresh-token")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "refresh token expired"


def test_refresh_revoked_jti_rejected(auth_ns, make_session_cookie):
    signed, _jti = make_session_cookie(user_id=5)
    auth_ns.client.cookies.set("auth_token", signed)
    # Nothing seeded in refresh_redis -> jti considered revoked.

    resp = auth_ns.client.post("/api/v1/auth/refresh-token")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "refresh token revoked"


def test_refresh_unknown_user_rejected(auth_ns, make_session_cookie):
    signed, jti = make_session_cookie(user_id=123)
    auth_ns.client.cookies.set("auth_token", signed)
    auth_ns.refresh_redis.store[f"refresh:{jti}"] = "123"
    auth_ns.db.get_results[123] = None  # user vanished

    resp = auth_ns.client.post("/api/v1/auth/refresh-token")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "user not found"


def test_refresh_rotates_tokens(auth_ns, make_session_cookie, make_user):
    user = make_user(uid=9, email="rot@x.com")
    signed, old_jti = make_session_cookie(user_id=9, email="rot@x.com")
    auth_ns.client.cookies.set("auth_token", signed)
    auth_ns.refresh_redis.store[f"refresh:{old_jti}"] = "9"
    auth_ns.db.get_results[9] = user

    resp = auth_ns.client.post(
        "/api/v1/auth/refresh-token",
        headers={"user-agent": "pytest-agent"},
    )

    assert resp.status_code == 200
    assert resp.json()["detail"] == "tokens refreshed"

    # Old jti revoked, fresh pair issued and stored.
    assert f"refresh:{old_jti}" in auth_ns.refresh_redis.deleted

    set_cookies = " ".join(resp.headers.get_list("set-cookie"))
    assert "auth_token=" in set_cookies

    new_keys = [
        k for k in auth_ns.refresh_redis.store if k.startswith("refresh:")
    ]
    assert len(new_keys) == 1
    new_jti = new_keys[0].split(":", 1)[1]
    assert new_jti != old_jti
