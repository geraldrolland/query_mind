"""GET /api/v1/auth/google-auth (OAuth callback).

All tests disabled — Google OAuth route removed.
"""

# import urllib.parse
#
# from app.routers import auth as auth_mod
#
# FRONTEND = "http://frontend.test"
#
#
# def configure_google(monkeypatch):
#     monkeypatch.setattr(auth_mod.settings, "APP_HOST", FRONTEND)
#     monkeypatch.setattr(auth_mod.settings, "GOOGLE_CLIENT_ID", "cid")
#     monkeypatch.setattr(auth_mod.settings, "GOOGLE_CLIENT_SECRET", "secret")
#
#
# def _location(resp) -> str:
#     return resp.headers["location"]
#
#
# def test_google_auth_error_param_redirects_to_signin(auth_ns, monkeypatch):
#     configure_google(monkeypatch)
#     resp = auth_ns.client.get(
#         "/api/v1/auth/google-auth?state=s&error=access_denied",
#         follow_redirects=False,
#     )
#     assert resp.status_code == 307
#     assert _location(resp).startswith(f"{FRONTEND}/signin?msg=google%20authentication%20failed")
#
#
# def test_google_auth_missing_code_redirects(auth_ns, monkeypatch):
#     configure_google(monkeypatch)
#     resp = auth_ns.client.get(
#         "/api/v1/auth/google-auth?state=s", follow_redirects=False
#     )
#     assert resp.status_code == 307
#     assert "msg=google%20authentication%20failed" in _location(resp)
#
#
# def test_google_auth_unconfigured_redirects_with_message(auth_ns, monkeypatch):
#     configure_google(monkeypatch)
#     monkeypatch.setattr(auth_mod.settings, "GOOGLE_CLIENT_ID", "")
#
#     resp = auth_ns.client.get(
#         "/api/v1/auth/google-auth?state=s&code=c", follow_redirects=False
#     )
#     assert "msg=google%20auth%20not%20configured" in _location(resp)
#
#
# def test_google_auth_invalid_state_rejected(auth_ns, monkeypatch):
#     configure_google(monkeypatch)
#
#     resp = auth_ns.client.get(
#         "/api/v1/auth/google-auth?state=unknown-state&code=c",
#         follow_redirects=False,
#     )
#     assert "msg=invalid%20oauth%20state" in _location(resp)
#
#
# def test_google_auth_state_is_single_use(auth_ns, monkeypatch):
#     configure_google(monkeypatch)
#     auth_ns.redis.store["oauth_state:one-shot"] = "1"
#     async def fail_exchange(code):
#         return None
#
#     monkeypatch.setattr(
#         auth_mod, "exchange_code_for_user", fail_exchange
#     )  # exchange fails, but state is already consumed
#
#     first = auth_ns.client.get(
#         "/api/v1/auth/google-auth?state=one-shot&code=c", follow_redirects=False
#     )
#     second = auth_ns.client.get(
#         "/api/v1/auth/google-auth?state=one-shot&code=c", follow_redirects=False
#     )
#
#     assert "oauth_state:one-shot" not in auth_ns.redis.store
#     assert "msg=invalid%20oauth%20state" in _location(second)
#
#
# def test_google_auth_exchange_failure_redirects(auth_ns, monkeypatch):
#     configure_google(monkeypatch)
#     auth_ns.redis.store["oauth_state:st"] = "1"
#
#     async def fail_exchange(code):
#         return None
#
#     monkeypatch.setattr(auth_mod, "exchange_code_for_user", fail_exchange)
#
#     resp = auth_ns.client.get(
#         "/api/v1/auth/google-auth?state=st&code=c", follow_redirects=False
#     )
#     assert "msg=google%20authentication%20failed" in _location(resp)
#
#
# def test_google_auth_success_creates_user_and_issues_session_code(
#     auth_ns, monkeypatch, make_user
# ):
#     configure_google(monkeypatch)
#     auth_ns.redis.store["oauth_state:good"] = "1"
#
#     async def ok_exchange(code):
#         return {"email": "g@gmail.com", "name": "G User"}
#
#     monkeypatch.setattr(auth_mod, "exchange_code_for_user", ok_exchange)
#     auth_ns.db.query_results.append([])  # no existing user -> created
#
#     resp = auth_ns.client.get(
#         "/api/v1/auth/google-auth?state=good&code=auth-code",
#         follow_redirects=False,
#         headers={"user-agent": "pytest-agent"},
#     )
#
#     assert resp.status_code == 307
#     location = _location(resp)
#     assert location.startswith(f"{FRONTEND}/account?")
#     query = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
#     session_code = query["session"][0]
#
#     # New google user persisted: verified by default, provider google.
#     created = auth_ns.db.added[0]
#     assert created.email == "g@gmail.com"
#     assert created.auth_provider == "google"
#     assert created.is_email_verified is True
#
#     # Short-lived opaque session code stored for the frontend to exchange.
#     assert auth_ns.redis.store[f"oauth_session:{session_code}"] == str(created.id)
#
#
# def test_google_auth_success_reuses_existing_user(auth_ns, monkeypatch, make_user):
#     configure_google(monkeypatch)
#     auth_ns.redis.store["oauth_state:reuse"] = "1"
#     existing = make_user(uid=12, email="existing@gmail.com", provider="email")
#
#     async def reuse_exchange(code):
#         return {"email": "existing@gmail.com"}
#
#     monkeypatch.setattr(auth_mod, "exchange_code_for_user", reuse_exchange)
#     auth_ns.db.query_results.append([existing])
#
#     resp = auth_ns.client.get(
#         "/api/v1/auth/google-auth?state=reuse&code=c",
#         follow_redirects=False,
#         headers={"user-agent": "pytest-agent"},
#     )
#
#     assert resp.status_code == 307
#     location = _location(resp)
#     query = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
#     session_code = query["session"][0]
#     # No duplicate user created; existing id bound to the session code.
#     assert auth_ns.db.added == []
#     assert auth_ns.redis.store[f"oauth_session:{session_code}"] == "12"
