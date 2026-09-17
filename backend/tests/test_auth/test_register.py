"""POST /api/v1/auth/register."""

from app.auth.utils import verify_password


def test_register_success_creates_unverified_user(
    auth_ns, patch_send_verification, make_user
):
    send_link = patch_send_verification()

    resp = auth_ns.client.post(
        "/api/v1/auth/register",
        json={
            "email": "new@x.com",
            "password": "Str0ng!pass",
            "confirm_password": "Str0ng!pass",
        },
    )

    assert resp.status_code == 201
    assert resp.json() == {
        "detail": "user registered successfully",
        "verification_required": True,
    }
    assert send_link.count == 1
    assert send_link.calls[0][0][0] == "new@x.com"

    user = auth_ns.db.added[0]
    assert user.email == "new@x.com"
    assert user.is_email_verified is False
    assert user.auth_provider == "email"
    # Password stored hashed, never in plaintext.
    assert user.hashed_password != "Str0ng!pass"
    assert verify_password("Str0ng!pass", user.hashed_password)
    assert auth_ns.db.commits == 1


def test_register_duplicate_email_rejected(auth_ns, patch_send_verification, make_user):
    send_link = patch_send_verification()
    existing = make_user(email="taken@x.com")
    auth_ns.db.query_results.append([existing])

    resp = auth_ns.client.post(
        "/api/v1/auth/register",
        json={
            "email": "taken@x.com",
            "password": "Str0ng!pass",
            "confirm_password": "Str0ng!pass",
        },
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "user with this email already exists"
    assert auth_ns.db.added == []
    assert send_link.count == 0


def _payload(**overrides):
    body = {
        "email": "weak@x.com",
        "password": "Str0ng!pass",
        "confirm_password": "Str0ng!pass",
    }
    body.update(overrides)
    return body


def test_register_weak_password_rejected(auth_ns, patch_send_verification):
    for bad in ("alllowercase1!", "NOLOWER1!", "NoDigits!!", "NoSpecial11A"):
        resp = auth_ns.client.post(
            "/api/v1/auth/register",
            json=_payload(password=bad, confirm_password=bad),
        )
        assert resp.status_code == 422, bad


def test_register_password_mismatch_rejected(auth_ns, patch_send_verification):
    resp = auth_ns.client.post(
        "/api/v1/auth/register",
        json=_payload(confirm_password="D1fferent!pass"),
    )
    assert resp.status_code == 422


def test_register_invalid_email_format_rejected(auth_ns, patch_send_verification):
    resp = auth_ns.client.post(
        "/api/v1/auth/register", json=_payload(email="not-an-email")
    )
    assert resp.status_code == 422


def test_register_short_password_rejected(auth_ns, patch_send_verification):
    resp = auth_ns.client.post(
        "/api/v1/auth/register",
        json=_payload(password="S1!a", confirm_password="S1!a"),
    )
    assert resp.status_code == 422


def test_send_verification_link_stores_token_and_logs_when_send_fails(
    auth_ns, monkeypatch, caplog
):
    import logging

    from app.routers import auth as auth_mod

    monkeypatch.setattr(auth_mod.settings, "APP_HOST", "http://frontend.test")

    monkeypatch.setattr(auth_mod, "send_email", lambda *a, **k: False)

    with caplog.at_level(logging.INFO, logger="app.routers.auth"):
        auth_mod._send_verification_link("v@x.com", auth_ns.db)

    # A verify:<token> key was stored in redis mapping to the email.
    keys = [k for k in auth_ns.redis.store if k.startswith("verify:")]
    assert len(keys) == 1
    token = keys[0].split(":", 1)[1]
    assert auth_ns.redis.store[keys[0]] == "v@x.com"

    expected_fragment = (
        f"EMAIL_VERIFICATION_LINK: http://frontend.test/verify-email"
        f"?token={token}&email=v@x.com"
    )
    rendered = [
        record.getMessage() for record in caplog.records
    ]
    assert any(expected_fragment in message for message in rendered)
