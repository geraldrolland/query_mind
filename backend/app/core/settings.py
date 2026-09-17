"""Application settings loaded from environment variables.

All configuration is driven by a single ``Settings`` pydantic model
that reads from a ``.env`` file (or real environment variables in
production). The module-level ``settings`` singleton is safe to import
anywhere; ``get_settings()`` is also exposed for test overriding.
"""

from functools import lru_cache
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration model for the QueryMind backend.

    Attributes:
        APP_NAME: Human-readable service name shown in docs and logs.
        APP_HOST: Base URL of the frontend (used in e-mail links).
        SECRET_KEY: HMAC signing key for JWTs; **must** be overridden
            in production.
        JWT_ACCESS_EXPIRE_MINUTES: Lifetime of access tokens.
        JWT_REFRESH_EXPIRE_DAYS: Lifetime of refresh tokens.
        COOKIE_SECURE: Send cookies only over HTTPS.
        COOKIE_SAMESITE: SameSite policy for auth cookies.
        COOKIE_PARTITIONED: Enable CHIPS partitioned cookies.
        COOKIE_HTTPONLY: Prevent JavaScript access to auth cookies.
        CORS_ORIGINS: List of allowed CORS origins.
        DATABASE_URL: SQLAlchemy connection string for development.
        PROD_DATABASE_URL: Production database connection string;
            falls back to ``DATABASE_URL`` when empty.
        REDIS_URL: Redis URL for query caching.
        PROD_REDIS_URL: Production Redis URL; falls back to
            ``REDIS_URL`` when empty.
        REDIS_REFRESH_DB: Redis database index for refresh tokens.
        REDIS_TOKEN: Upstash Redis auth token (production only).
        CACHE_EXPIRE_SECONDS: TTL for cached query results.
        ENVIRONMENT: Runtime environment (``development`` or
            ``production``).
        GOOGLE_CLIENT_ID: OAuth 2.0 client ID.
        GOOGLE_CLIENT_SECRET: OAuth 2.0 client secret.
        GOOGLE_REDIRECT_URI: Callback URL for the Google OAuth flow.
        GOOGLE_AUTH_URL: Google OAuth 2.0 authorization endpoint.
        GOOGLE_TOKEN_URL: Google OAuth 2.0 token exchange endpoint.
        GOOGLE_USERINFO_URL: Google endpoint to fetch user profile.
        LLM_BASE_URL: Base URL of the LLM inference server.
        LLM_API_KEY: API key for the LLM provider.
        LLM_MODEL: Model identifier to use for completions.
        LLM_MAX_TOKENS: Maximum tokens in a single completion.
        LLM_TEMPERATURE: Sampling temperature (0.0 - 2.0).
        LLM_TIMEOUT_SECONDS: Request timeout for LLM calls.
        MAX_TOOL_ITERATIONS: Maximum MCP tool-call iterations per turn.
        SMTP_HOST: SMTP server hostname.
        SMTP_PORT: SMTP server port.
        SMTP_USERNAME: SMTP authentication username.
        SMTP_PASSWORD: SMTP authentication password.
        SMTP_FROM_EMAIL: Sender address on outgoing e-mails.
        SMTP_FROM_NAME: Sender display name.
        SMTP_USE_STARTTLS: Upgrade connection with STARTTLS.
        SMTP_USE_SSL: Connect over implicit TLS.
        SENDGRID_API_KEY: SendGrid API key; when set, SendGrid is
            preferred over SMTP for outbound e-mail.
        MAX_UPLOAD_BYTES: Maximum CSV upload size in bytes (50 MB).
    """

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    APP_NAME: str = "QueryMind"
    APP_HOST: str = "http://localhost:3000"
    SECRET_KEY: str = "dev-secret-key-change-me"
    JWT_ACCESS_EXPIRE_MINUTES: int = 15
    JWT_REFRESH_EXPIRE_DAYS: int = 7
    COOKIE_SECURE: bool = False
    COOKIE_SAMESITE: str = "lax"
    COOKIE_PARTITIONED: bool = False
    COOKIE_HTTPONLY: bool = True
    CORS_ORIGINS: List[str] = [
        "http://localhost:3000", 
        "http://localhost:3001"
        ]

    DATABASE_URL: str = (
        "postgresql://postgres:querymind123@localhost:5433/querymind"
    )

    PROD_DATABASE_URL: str = ""

    REDIS_URL: str = "redis://localhost:6380/0"
    PROD_REDIS_URL: str = ""    
    REDIS_REFRESH_DB: int = 1
    REDIS_TOKEN: str = ""
    CACHE_EXPIRE_SECONDS: int = 300
    ENVIRONMENT: str = "development"

    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = (
        "http://localhost:8000/api/v1/auth/google-auth"
    )
    GOOGLE_AUTH_URL: str = "https://accounts.google.com/o/oauth2/v2/auth"
    GOOGLE_TOKEN_URL: str = "https://oauth2.googleapis.com/token"
    GOOGLE_USERINFO_URL: str = "https://www.googleapis.com/oauth2/v2/userinfo"

    LLM_BASE_URL: str = ""
    LLM_API_KEY: str = ""
    LLM_MODEL: str = ""
    LLM_MAX_TOKENS: int = 4096
    LLM_TEMPERATURE: float = 0.7
    LLM_TIMEOUT_SECONDS: int = 90
    MAX_TOOL_ITERATIONS: int = 10

    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM_EMAIL: str = "no-reply@querymind.app"
    SMTP_FROM_NAME: str = "QueryMind"
    SMTP_USE_STARTTLS: bool = True
    SMTP_USE_SSL: bool = False

    SENDGRID_API_KEY: str = ""

    REVIEW_EMAIL: str = ""

    MAX_UPLOAD_BYTES: int = 50 * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    """Return a cached singleton ``Settings`` instance.

    Uses ``functools.lru_cache`` so repeated imports resolve to the
    same object and the ``.env`` file is parsed exactly once per
    process lifetime.  Call ``get_settings.cache_clear()`` in tests
    to pick up overridden env vars.
    """
    return Settings()


settings = get_settings()
