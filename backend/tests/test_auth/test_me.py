"""GET /api/v1/auth/me."""


def test_me_returns_state_user_profile(auth_ns):
    auth_ns.state["auth_user"] = {"id": 42, "email": "me@x.com"}

    resp = auth_ns.client.get("/api/v1/auth/me")

    assert resp.status_code == 200
    assert resp.json() == {"id": 42, "email": "me@x.com", "auth_provider": "email"}


def test_me_reflects_current_session_user(auth_ns):
    auth_ns.state["auth_user"] = {"id": 99, "email": "someone@else.com"}

    resp = auth_ns.client.get("/api/v1/auth/me")

    body = resp.json()
    assert body["id"] == 99
    assert body["email"] == "someone@else.com"
