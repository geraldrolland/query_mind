"""Endpoint tests for app/routers/auth.py — one file per endpoint.

Layout:
    conftest.py                        shared app/client/fakes
    test_register.py                   POST /api/v1/auth/register
    test_login.py                      POST /api/v1/auth/login
    test_me.py                         GET  /api/v1/auth/me
    test_refresh_token.py              POST /api/v1/auth/refresh-token
    test_logout.py                     POST /api/v1/auth/logout
    test_request_verification_email.py POST /api/v1/auth/email
    test_verify_email.py               POST /api/v1/auth/verify-email
    test_request_password_reset.py     POST /api/v1/auth/reset-password
    test_confirm_password_reset.py     POST /api/v1/auth/reset-password/confirm
    test_google_url.py                 GET  /api/v1/auth/google/url
    test_google_auth.py                GET  /api/v1/auth/google-auth
    test_exchange_session.py           POST /api/v1/auth/session

pytest does not auto-collect ``__init__.py``, but the sibling ``test_*.py``
files are collected normally:

    python -m pytest tests/test_auth/ -v
"""
