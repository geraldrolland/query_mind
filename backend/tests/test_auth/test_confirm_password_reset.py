"""POST /api/v1/auth/reset-password/confirm."""

from app.auth.utils import verify_password


def test_confirm_reset_updates_password_and_consumes_token(auth_ns, make_user):
    user = make_user(uid=8, email="r@x.com", password_hash="old-hash")
    auth_ns.db.query_results.append([user])
    auth_ns.redis.store["reset:tok1"] = "r@x.com"

    resp = auth_ns.client.post(
        "/api/v1/auth/reset-password/confirm",
        json={
            "email": "r@x.com",
            "token": "tok1",
            "password": "N3w!password",
            "confirm_password": "N3w!password",
        },
    )

    assert resp.status_code == 200
    assert resp.json()["detail"] == "password reset successfully"
    assert user.hashed_password != "old-hash"
    assert verify_password("N3w!password", user.hashed_password)
    assert "reset:tok1" not in auth_ns.redis.store
    assert "reset:tok1" in auth_ns.redis.deleted


def test_confirm_reset_invalid_token_rejected(auth_ns):
    resp = auth_ns.client.post(
        "/api/v1/auth/reset-password/confirm",
        json={
            "email": "r@x.com",
            "token": "bogus",
            "password": "N3w!password",
            "confirm_password": "N3w!password",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid or expired token"


def test_confirm_reset_email_mismatch_rejected(auth_ns, make_user):
    user = make_user(uid=9)
    auth_ns.db.query_results.append([user])
    auth_ns.redis.store["reset:tok2"] = "owner@x.com"

    resp = auth_ns.client.post(
        "/api/v1/auth/reset-password/confirm",
        json={
            "email": "attacker@x.com",  # token belongs to another account
            "token": "tok2",
            "password": "N3w!password",
            "confirm_password": "N3w!password",
        },
    )
    assert resp.status_code == 400


def test_confirm_reset_user_not_found(auth_ns):
    auth_ns.redis.store["reset:tok3"] = "gone@x.com"
    auth_ns.db.query_results.append([])
    resp = auth_ns.client.post(
        "/api/v1/auth/reset-password/confirm",
        json={
            "email": "gone@x.com",
            "token": "tok3",
            "password": "N3w!password",
            "confirm_password": "N3w!password",
        },
    )
    assert resp.status_code == 404


def test_confirm_reset_password_mismatch_rejected(auth_ns):
    resp = auth_ns.client.post(
        "/api/v1/auth/reset-password/confirm",
        json={
            "email": "r@x.com",
            "token": "tok4",
            "password": "N3w!password",
            "confirm_password": "D1fferent!pass",
        },
    )
    assert resp.status_code == 422
