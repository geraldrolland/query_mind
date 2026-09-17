"""POST /api/v1/auth/login."""

from app.auth.utils import hash_password


def test_login_success_sets_session_cookies(auth_ns, make_user):
    user = make_user(
        uid=3,
        email="ok@x.com",
        verified=True,
        password_hash=hash_password("Str0ng!pass"),
    )
    auth_ns.db.query_results.append([user])

    resp = auth_ns.client.post(
        "/api/v1/auth/login",
        json={"email": "ok@x.com", "password": "Str0ng!pass"},
    )

    assert resp.status_code == 200
    assert resp.json()["detail"] == "user logged in successfully"

    set_cookies = " ".join(resp.headers.get_list("set-cookie"))
    assert "auth_token=" in set_cookies
    assert "csrf_token=" in set_cookies

    # Refresh jti stored in Redis for rotation/revocation.
    refresh_keys = [k for k in auth_ns.refresh_redis.store if k.startswith("refresh:")]
    assert len(refresh_keys) == 1
    assert auth_ns.refresh_redis.store[refresh_keys[0]] == "3"


def test_login_wrong_password_rejected(auth_ns, make_user):
    user = make_user(uid=1, password_hash=hash_password("Str0ng!pass"))
    auth_ns.db.query_results.append([user])

    resp = auth_ns.client.post(
        "/api/v1/auth/login", json={"email": "u@x.com", "password": "Wr0ng!pass"}
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid email or password"


def test_login_unknown_email_rejected(auth_ns):
    auth_ns.db.query_results.append([])  # no user found

    resp = auth_ns.client.post(
        "/api/v1/auth/login", json={"email": "ghost@x.com", "password": "whatever1!"}
    )
    assert resp.status_code == 401


def test_login_google_account_without_password_rejected(auth_ns, make_user):
    user = make_user(uid=2, provider="google", password_hash=None, verified=True)
    auth_ns.db.query_results.append([user])

    resp = auth_ns.client.post(
        "/api/v1/auth/login", json={"email": "u@x.com", "password": "whatever1!"}
    )
    assert resp.status_code == 401


def test_login_unverified_resends_link_and_returns_403(
    auth_ns, patch_send_verification, make_user
):
    send_link = patch_send_verification()
    user = make_user(
        uid=4,
        email="unv@x.com",
        verified=False,
        password_hash=hash_password("Str0ng!pass"),
    )
    auth_ns.db.query_results.append([user])

    resp = auth_ns.client.post(
        "/api/v1/auth/login",
        json={"email": "unv@x.com", "password": "Str0ng!pass"},
    )

    assert resp.status_code == 403
    assert resp.json()["detail"] == "email not verified, verification link sent"
    assert send_link.count == 1
    assert send_link.calls[0][0][0] == "unv@x.com"
