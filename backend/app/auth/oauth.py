"""Google OAuth2 code-exchange flow."""

import httpx

from app.core.settings import settings
from app.auth.utils import decode_token


def build_google_auth_url(state: str) -> str:
    """Build the Google OAuth consent-screen URL for the sign-in redirect.

    Args:
        state: Opaque CSRF token previously stored in Redis; Google echoes it
            back on the callback so the handler can verify browser round-trip.

    Returns:
        Fully-qualified authorization URL carrying client id, redirect URI,
        scopes (email + profile), offline access, and the state parameter.
    """
    params = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": settings.GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "https://www.googleapis.com/auth/userinfo.email https://www.googleapis.com/auth/userinfo.profile",
        "prompt": "select_account",
        "access_type": "offline",
        "state": state,
    }
    import urllib.parse

    return f"{settings.GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"


async def exchange_code_for_user(code: str) -> dict | None:
    """Exchange the OAuth code for a Google user profile."""
    async with httpx.AsyncClient(timeout=15) as client:
        token_resp = await client.post(
            settings.GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "redirect_uri": settings.GOOGLE_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
        )
        if token_resp.status_code != 200:
            return None
        token_data = token_resp.json()

        user_resp = await client.get(
            settings.GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {token_data['access_token']}"},
        )
        if user_resp.status_code != 200:
            return None
        return user_resp.json()


def check_user_agent_match(token_user_agent: str, request_user_agent: str) -> bool:
    """Check that the session token's bound user agent matches the request.

    Args:
        token_user_agent: User agent recorded when the token pair was minted.
            ``None`` means the binding is unknown and access is denied.
        request_user_agent: User agent presented by the current client.

    Returns:
        True only when both values are present and identical; False otherwise
        (including when ``token_user_agent`` is None).
    """
    if token_user_agent is None:
        return False
    import hashlib

    return hmac_compare(token_user_agent, request_user_agent)


def hmac_compare(a: str, b: str) -> bool:
    """Constant-time string equality via :func:`hmac.compare_digest`.

    Prevents timing attacks when comparing secrets such as user-agent
    fingerprints or CSRF tokens.

    Args:
        a: First string.
        b: Second string.

    Returns:
        True when both strings are equal, False otherwise.
    """
    import hmac as _hmac

    return _hmac.compare_digest(a, b)
