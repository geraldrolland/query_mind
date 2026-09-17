"""POST /api/v1/auth/logout."""


def test_logout_revokes_jti_and_clears_cookies(auth_ns, make_session_cookie):
    signed, jti = make_session_cookie(user_id=3)
    auth_ns.refresh_redis.store[f"refresh:{jti}"] = "3"
    auth_ns.client.cookies.set("auth_token", signed)

    resp = auth_ns.client.post("/api/v1/auth/logout")

    assert resp.status_code == 200
    assert resp.json()["detail"] == "logged out"
    assert f"refresh:{jti}" in auth_ns.refresh_redis.deleted

    set_cookies = " ".join(resp.headers.get_list("set-cookie"))
    # Deletion cookies carry an expired/empty value for both names.
    assert "auth_token=" in set_cookies
    assert "csrf_token=" in set_cookies


def test_logout_without_cookie_still_succeeds(auth_ns):
    resp = auth_ns.client.post("/api/v1/auth/logout")
    assert resp.status_code == 200
    assert resp.json()["detail"] == "logged out"


def test_logout_with_garbage_cookie_still_succeeds(auth_ns):
    auth_ns.client.cookies.set("auth_token", "garbage-value")
    resp = auth_ns.client.post("/api/v1/auth/logout")
    assert resp.status_code == 200
