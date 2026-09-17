"""POST /api/v1/auth/verify-email."""


def test_verify_email_success(auth_ns, make_user):
    user = make_user(uid=5, email="v@x.com", verified=False)
    auth_ns.db.query_results.append([user])
    auth_ns.redis.store["verify:tok123"] = "v@x.com"

    resp = auth_ns.client.post(
        "/api/v1/auth/verify-email",
        json={"email": "v@x.com", "token": "tok123"},
    )

    assert resp.status_code == 200
    assert resp.json()["detail"] == "email verified successfully"
    assert user.is_email_verified is True
    assert auth_ns.db.commits >= 1
    # Token consumed (single-use).
    assert "verify:tok123" not in auth_ns.redis.store
    assert "verify:tok123" in auth_ns.redis.deleted


def test_verify_email_token_email_mismatch_rejected(auth_ns, make_user):
    user = make_user(uid=6, email="v@x.com", verified=False)
    auth_ns.db.query_results.append([user])
    auth_ns.redis.store["verify:tok456"] = "other@x.com"

    resp = auth_ns.client.post(
        "/api/v1/auth/verify-email",
        json={"email": "v@x.com", "token": "tok456"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid or expired token"
    assert user.is_email_verified is False


def test_verify_email_unknown_or_expired_token_rejected(auth_ns):
    resp = auth_ns.client.post(
        "/api/v1/auth/verify-email",
        json={"email": "v@x.com", "token": "missing-token"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid or expired token"


def test_verify_email_user_not_found_after_valid_token(auth_ns):
    auth_ns.redis.store["verify:tok789"] = "ghost@x.com"
    auth_ns.db.query_results.append([])  # lookup misses

    resp = auth_ns.client.post(
        "/api/v1/auth/verify-email",
        json={"email": "ghost@x.com", "token": "tok789"},
    )
    assert resp.status_code == 404
