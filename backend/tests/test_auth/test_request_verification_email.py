"""POST /api/v1/auth/email (request verification email)."""


def test_request_verification_sends_link(auth_ns, make_user):
    user = make_user(uid=2, email="v@x.com", verified=False)
    auth_ns.db.query_results.append([user])

    resp = auth_ns.client.post("/api/v1/auth/email", json={"email": "v@x.com"})

    assert resp.status_code == 200
    assert resp.json()["detail"] == "verification link sent"
    keys = [k for k in auth_ns.redis.store if k.startswith("verify:")]
    assert len(keys) == 1
    assert auth_ns.redis.store[keys[0]] == "v@x.com"


def test_request_verification_already_verified(auth_ns, make_user):
    user = make_user(uid=3, verified=True)
    auth_ns.db.query_results.append([user])

    resp = auth_ns.client.post("/api/v1/auth/email", json={"email": "u@x.com"})

    assert resp.status_code == 200
    assert resp.json()["detail"] == "email already verified"
    assert not [k for k in auth_ns.redis.store if k.startswith("verify:")]


def test_request_verification_unknown_user_404(auth_ns):
    auth_ns.db.query_results.append([])
    resp = auth_ns.client.post("/api/v1/auth/email", json={"email": "no@x.com"})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "user not found"
