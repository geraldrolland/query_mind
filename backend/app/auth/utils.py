"""JWT generation/verification, password hashing, and cookie/CSRF helpers."""

import base64
import hashlib
import hmac
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from itsdangerous import BadSignature, TimestampSigner
from pwdlib import PasswordHash

from app.core.settings import settings

logger = logging.getLogger(__name__)

password_hash = PasswordHash.recommended()
cookie_signer = TimestampSigner(settings.SECRET_KEY)


def hash_password(password: str) -> str:
    """Hash a plaintext password with Argon2 (via pwdlib's recommended hash).

    Args:
        password: Plaintext password chosen by the user.

    Returns:
        Encoded hash string (algorithm parameters embedded) suitable for
        storage in the ``hashed_password`` column.
    """
    return password_hash.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    """Check a plaintext password against a stored Argon2 hash.

    Args:
        password: Plaintext candidate presented at login.
        hashed: Stored hash string produced by :func:`hash_password`.

    Returns:
        True when the candidate matches; False on mismatch or if the stored
        value is malformed/unhashable (never raises to the caller).
    """
    try:
        return password_hash.verify(password, hashed)
    except Exception:
        return False


def generate_access_token(payload: dict) -> str:
    """Mint a short-lived HS256 access JWT.

    The ``type`` claim is forced to ``access_token`` and standard ``exp`` /
    ``iat`` claims are added based on ``JWT_ACCESS_EXPIRE_MINUTES``.

    Args:
        payload: User claims to embed (typically ``sub``, ``email``,
            ``user_agent``).

    Returns:
        Signed, URL-safe JWT string for the client cookie.
    """
    data = {
        **payload,
        "type": "access_token",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=settings.JWT_ACCESS_EXPIRE_MINUTES),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(data, settings.SECRET_KEY, algorithm="HS256")


def generate_refresh_token(payload: dict) -> str:
    """Mint a long-lived HS256 refresh JWT with a unique ``jti``.

    The ``type`` claim is forced to ``refresh_token``; expiry comes from
    ``JWT_REFRESH_EXPIRE_DAYS``. The random ``jti`` is what the backend
    stores in Redis to support rotation and revocation.

    Args:
        payload: User claims to embed (typically ``sub``, ``email``,
            ``user_agent``).

    Returns:
        Signed, URL-safe JWT string containing a fresh 32-char hex ``jti``.
    """
    data = {
        **payload,
        "type": "refresh_token",
        "jti": secrets.token_hex(16),
        "exp": datetime.now(timezone.utc) + timedelta(days=settings.JWT_REFRESH_EXPIRE_DAYS),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(data, settings.SECRET_KEY, algorithm="HS256")


def decode_token(token: str, expected_type: str = "access_token") -> dict | None:
    """Verify signature/expiry of a JWT and check its ``type`` claim.

    Args:
        token: Encoded JWT previously produced by :func:`generate_access_token`
            or :func:`generate_refresh_token`.
        expected_type: Required value of the ``type`` claim, preventing an
            access token from being replayed as a refresh token (or
            vice-versa).

    Returns:
        The decoded claim dict on success; ``None`` for any failure — bad
        signature, expired, malformed, or type mismatch. Never raises.
    """
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
        if payload.get("type") != expected_type:
            return None
        return payload
    except jwt.PyJWTError:
        return None


def sign_cookie(value: dict) -> str:
    """Sign a JSON dict into an opaque cookie value."""
    raw = json.dumps(value, separators=(",", ":")).encode()
    return cookie_signer.sign(raw).decode()


def unsign_cookie(value: str, max_age: int | None = None) -> dict | None:
    """Verify and decode a signed cookie value back into its original dict.

    Args:
        value: Opaque cookie string produced by :func:`sign_cookie`.
        max_age: Maximum acceptable age in seconds; ``None`` disables the
            timestamp check (used for refresh flows where the JWT itself
            carries expiry).

    Returns:
        The original JSON dict on success; ``None`` when the signature is
        invalid/tampered, the value expired, or the payload is not JSON.
        Never raises.
    """
    try:
        raw = cookie_signer.unsign(value, max_age=max_age)
        return json.loads(raw.decode())
    except (BadSignature, json.JSONDecodeError, TypeError):
        return None


def gen_csrf_token() -> str:
    """Generate a cryptographically random CSRF token.

    Returns:
        URL-safe random string (43 chars, 256 bits of entropy) suitable for
        pairing with :func:`sign_csrf`.
    """
    return secrets.token_urlsafe(32)


def sign_csrf(token: str) -> str:
    """Sign a CSRF token so it can be stored in a cookie untrusted by clients.

    Args:
        token: Raw token from :func:`gen_csrf_token` (or any string).

    Returns:
        Timestamped signature string; verification is done via
        :func:`verify_csrf`, which also enforces the signing age.
    """
    return cookie_signer.sign(token.encode()).decode()


def verify_csrf(signed: str, provided: str, max_age: int | None = 3600) -> bool:
    """Verify a CSRF token: signed value matches and equals header value."""
    if not signed or not provided:
        return False
    try:
        raw = cookie_signer.unsign(signed, max_age=max_age).decode()
    except BadSignature:
        return False
    return hmac.compare_digest(raw, provided)


def make_tokens(user_id: int, email: str, user_agent: str) -> dict:
    """Create access+refresh token pair for a user."""
    access = generate_access_token({"sub": str(user_id), "email": email, "user_agent": user_agent})
    refresh = generate_refresh_token({"sub": str(user_id), "email": email, "user_agent": user_agent})
    jti = decode_token(refresh, "refresh_token").get("jti")
    return {"access": access, "refresh": refresh, "jti": jti}
