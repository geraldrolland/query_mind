"""POST /api/v1/auth/reset-password (request reset link)."""


def test_request_reset_sends_link_and_stores_token(auth_ns, make_user):
    user = make_user(uid=4, email="r@x.com")
    auth_ns.db.query_results.append([user])

    resp = auth_ns.client.post("/api/v1/auth/reset-password", json={"email": "r@x.com"})

    assert resp.status_code == 200
    assert resp.json()["detail"] == "reset link sent"
    keys = [k for k in auth_ns.redis.store if k.startswith("reset:")]
    assert len(keys) == 1
    assert auth_ns.redis.store[keys[0]] == "r@x.com"


def test_request_reset_unknown_user_404(auth_ns):
    auth_ns.db.query_results.append([])
    resp = auth_ns.client.post("/api/v1/auth/reset-password", json={"email": "no@x.com"})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "user not found"
    assert auth_ns.redis.store == {}
