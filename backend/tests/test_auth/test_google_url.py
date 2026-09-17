"""GET /api/v1/auth/google/url.

All tests disabled — Google OAuth route removed.
"""

# import urllib.parse
#
#
# def test_google_url_returns_url_and_stores_state(auth_ns, monkeypatch):
#     monkeypatch.setattr(auth_ns.settings, "GOOGLE_CLIENT_ID", "cid-123")
#     monkeypatch.setattr(auth_ns.settings, "GOOGLE_CLIENT_SECRET", "secret-456")
#
#     resp = auth_ns.client.get("/api/v1/auth/google/url")
#
#     assert resp.status_code == 200
#     url = resp.json()["url"]
#     assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
#
#     params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
#     assert params["client_id"] == ["cid-123"]
#     state = params["state"][0]
#     # CSRF state persisted in redis for the callback to verify.
#     assert auth_ns.redis.store[f"oauth_state:{state}"] == "1"
#
#
# def test_google_url_unconfigured_returns_503(auth_ns, monkeypatch):
#     monkeypatch.setattr(auth_ns.settings, "GOOGLE_CLIENT_ID", "")
#     monkeypatch.setattr(auth_ns.settings, "GOOGLE_CLIENT_SECRET", "")
#
#     resp = auth_ns.client.get("/api/v1/auth/google/url")
#     assert resp.status_code == 503
#     assert resp.json()["detail"] == "google auth not configured"
