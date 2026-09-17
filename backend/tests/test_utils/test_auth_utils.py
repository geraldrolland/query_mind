"""Unit tests for app/auth/utils.py.

Run:
    python -m pytest tests/test_utils/ -v
"""

from datetime import datetime, timedelta, timezone

import jwt

from app.auth import utils as auth_utils
from app.core.settings import settings

# ===========================================================================
# Password hashing
# ===========================================================================


def test_hash_password_returns_hash_different_from_input():
    hashed = auth_utils.hash_password("s3cret-pass!")
    assert isinstance(hashed, str)
    assert hashed != "s3cret-pass!"
    assert len(hashed) > 0


def test_verify_password_correct_password():
    hashed = auth_utils.hash_password("s3cret-pass!")
    assert auth_utils.verify_password("s3cret-pass!", hashed) is True


def test_verify_password_wrong_password():
    hashed = auth_utils.hash_password("s3cret-pass!")
    assert auth_utils.verify_password("wrong-pass", hashed) is False


def test_verify_password_malformed_hash_returns_false():
    assert auth_utils.verify_password("s3cret-pass!", "not-a-valid-hash") is False


# ===========================================================================
# Token generation / decoding
# ===========================================================================


def test_generate_access_token_roundtrip():
    token = auth_utils.generate_access_token({"sub": "42", "email": "a@b.c"})
    payload = auth_utils.decode_token(token)
    assert payload is not None
    assert payload["sub"] == "42"
    assert payload["email"] == "a@b.c"
    assert payload["type"] == "access_token"


def test_generate_refresh_token_has_jti_and_type():
    token = auth_utils.generate_refresh_token({"sub": "42"})
    payload = auth_utils.decode_token(token, expected_type="refresh_token")
    assert payload is not None
    assert payload["type"] == "refresh_token"
    jti = payload["jti"]
    assert isinstance(jti, str)
    assert len(jti) == 32
    int(jti, 16)  # must be hex; raises ValueError otherwise


def test_decode_token_type_mismatch_returns_none():
    access = auth_utils.generate_access_token({"sub": "1"})
    assert auth_utils.decode_token(access, expected_type="refresh_token") is None


def test_decode_token_garbage_returns_none():
    assert auth_utils.decode_token("not.a.jwt") is None
    assert auth_utils.decode_token("") is None


def test_decode_token_expired_returns_none():
    expired = jwt.encode(
        {
            "sub": "1",
            "type": "access_token",
            "exp": datetime.now(timezone.utc) - timedelta(minutes=5),
        },
        settings.SECRET_KEY,
        algorithm="HS256",
    )
    assert auth_utils.decode_token(expired) is None


# ===========================================================================
# Cookie signing
# ===========================================================================


def test_sign_and_unsign_cookie_roundtrip():
    value = {"user_id": 7, "email": "u@x.com", "roles": ["admin"]}
    signed = auth_utils.sign_cookie(value)
    assert isinstance(signed, str)
    assert auth_utils.unsign_cookie(signed) == value


def test_unsign_cookie_tampered_signature_returns_none():
    signed = auth_utils.sign_cookie({"user_id": 7})
    tampered = signed[:-4] + "aaaa"
    assert auth_utils.unsign_cookie(tampered) is None


def test_unsign_cookie_garbage_returns_none():
    assert auth_utils.unsign_cookie("totally-not-signed") is None


def test_unsign_cookie_non_json_payload_returns_none():
    # Signed by the raw signer with non-JSON bytes -> JSONDecodeError path.
    signed_raw = auth_utils.cookie_signer.sign(b"not-json{").decode()
    assert auth_utils.unsign_cookie(signed_raw) is None


# ===========================================================================
# CSRF helpers
# ===========================================================================


def test_gen_csrf_token_format_and_uniqueness():
    tok1 = auth_utils.gen_csrf_token()
    tok2 = auth_utils.gen_csrf_token()
    assert isinstance(tok1, str)
    assert len(tok1) == 43  # secrets.token_urlsafe(32)
    assert tok1 != tok2


def test_verify_csrf_valid_roundtrip():
    token = auth_utils.gen_csrf_token()
    signed = auth_utils.sign_csrf(token)
    assert auth_utils.verify_csrf(signed, token) is True


def test_verify_csrf_max_age_none_accepted():
    token = auth_utils.gen_csrf_token()
    signed = auth_utils.sign_csrf(token)
    assert auth_utils.verify_csrf(signed, token, max_age=None) is True


def test_verify_csrf_wrong_provided_value():
    token = auth_utils.gen_csrf_token()
    signed = auth_utils.sign_csrf(token)
    assert auth_utils.verify_csrf(signed, "other-token") is False


def test_verify_csrf_empty_inputs():
    token = auth_utils.gen_csrf_token()
    signed = auth_utils.sign_csrf(token)
    assert auth_utils.verify_csrf("", token) is False
    assert auth_utils.verify_csrf(signed, "") is False


def test_verify_csrf_tampered_signature():
    token = auth_utils.gen_csrf_token()
    signed = auth_utils.sign_csrf(token)
    assert auth_utils.verify_csrf(signed[:-2] + "zz", token) is False


# ===========================================================================
# make_tokens
# ===========================================================================


def test_make_tokens_pair_shape():
    result = auth_utils.make_tokens(42, "u@x.com", "Mozilla/5.0")
    assert set(result.keys()) == {"access", "refresh", "jti"}
    access_payload = auth_utils.decode_token(result["access"])
    refresh_payload = auth_utils.decode_token(result["refresh"], "refresh_token")
    assert access_payload["sub"] == "42"
    assert access_payload["email"] == "u@x.com"
    assert access_payload["user_agent"] == "Mozilla/5.0"
    assert refresh_payload["sub"] == "42"
    assert refresh_payload["email"] == "u@x.com"
    assert refresh_payload["user_agent"] == "Mozilla/5.0"


def test_make_tokens_jti_matches_refresh_token():
    result = auth_utils.make_tokens(1, "u@x.com", "agent")
    refresh_payload = auth_utils.decode_token(result["refresh"], "refresh_token")
    assert result["jti"] == refresh_payload["jti"]
