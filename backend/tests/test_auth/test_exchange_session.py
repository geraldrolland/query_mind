"""POST /api/v1/auth/session (OAuth session-code exchange).

All tests disabled — Google OAuth and /session route removed.
"""

# from app.routers import auth as auth_mod


# def test_exchange_session_success_sets_cookies(auth_ns, make_user):
#     user = make_user(uid=21, email="g@gmail.com")
#     auth_ns.redis.store["oauth_session:code-1"] = "21"
#     auth_ns.db.get_results[21] = user
#
#     resp = auth_ns.client.post(
#         "/api/v1/auth/session",
#         json={"token": "code-1"},
#         headers={"user-agent": "pytest-agent"},
#     )
#
#     assert resp.status_code == 200
#     assert resp.json()["detail"] == "session established"
#
#     set_cookies = " ".join(resp.headers.get_list("set-cookie"))
#     assert "auth_token=" in set_cookies
#     assert "csrf_token=" in set_cookies
#
#     # Code consumed after a single use.
#     assert "oauth_session:code-1" not in auth_ns.redis.store
#     assert "oauth_session:code-1" in auth_ns.redis.deleted
#
#
# def test_exchange_session_invalid_code_rejected(auth_ns):
#     resp = auth_ns.client.post("/api/v1/auth/session", json={"token": "bogus"})
#     assert resp.status_code == 400
#     assert resp.json()["detail"] == "invalid or expired session"
#
#
# def test_exchange_session_user_not_found_rejected(auth_ns):
#     auth_ns.redis.store["oauth_session:orphan"] = "999"
#     auth_ns.db.get_results[999] = None
#
#     resp = auth_ns.client.post("/api/v1/auth/session", json={"token": "orphan"})
#     assert resp.status_code == 404
#     assert resp.json()["detail"] == "user not found"
#
#
# def test_exchange_session_revocation_key_written(auth_ns, make_user, monkeypatch):
#     """The issued refresh jti is registered in redis for later revocation."""
#     user = make_user(uid=30)
#     auth_ns.redis.store["oauth_session:c"] = "30"
#     auth_ns.db.get_results[30] = user
#     monkeypatch.setattr(auth_mod.settings, "JWT_REFRESH_EXPIRE_DAYS", 3)
#
#     auth_ns.client.post("/api/v1/auth/session", json={"token": "c"})
#
#     refresh_keys = [k for k in auth_ns.refresh_redis.store if k.startswith("refresh:")]
#     assert len(refresh_keys) == 1
