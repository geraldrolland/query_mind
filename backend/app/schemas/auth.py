"""Pydantic request/response schemas for authentication endpoints.

These models define the wire contract for every auth-related route:
registration, login, email verification, password reset, session
exchange, and user profile output. Validation rules enforced here
(password complexity, email format, confirmation matching) are the
single source of truth for API documentation via OpenAPI / Swagger.
"""

from pydantic import BaseModel, Field, field_validator
from typing import Optional

# Shared e-mail pattern used across multiple schemas for consistency.
_EMAIL_PATTERN = r"^[\w\.-]+@[\w\.-]+\.\w{2,4}$"


class UserRegistrationSchema(BaseModel):
    """Payload for ``POST /auth/register``.

    Enforces password complexity (uppercase, lowercase, digit, special
    character) and confirmation matching before account creation.

    Attributes:
        email: Valid e-mail address; also serves as the login identifier.
        password: Plaintext password (8-128 chars, complexity enforced).
        confirm_password: Must equal ``password`` exactly.
    """

    email: str = Field(..., pattern=_EMAIL_PATTERN)
    password: str = Field(..., min_length=8, max_length=128)
    confirm_password: str = Field(..., min_length=8, max_length=128)

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        """Enforce password complexity rules.

        Args:
            v: The plaintext password to validate.

        Returns:
            The validated password unchanged.

        Raises:
            ValueError: If any complexity rule is violated.
        """
        if not any(c.isupper() for c in v):
            raise ValueError("Password must contain at least one uppercase letter")
        if not any(c.islower() for c in v):
            raise ValueError("Password must contain at least one lowercase letter")
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain at least one digit")
        if not any(c in "!@#$%^&*()-_=+[]{};:,.<>?/" for c in v):
            raise ValueError("Password must contain at least one special character")
        return v

    @field_validator("confirm_password")
    @classmethod
    def passwords_match(cls, v: str, info) -> str:
        """Cross-validate that confirmation matches password.

        Args:
            v: The confirmation password value.
            info: Pydantic ``ValidationInfo`` with earlier field values.

        Returns:
            The confirmation password unchanged.

        Raises:
            ValueError: If ``password`` and ``confirm_password`` differ.
        """
        if info.data.get("password") != v:
            raise ValueError("Passwords do not match")
        return v


class UserLoginSchema(BaseModel):
    """Payload for ``POST /auth/login``.

    Attributes:
        email: Registered e-mail address.
        password: Plaintext credential.
    """

    email: str = Field(..., pattern=_EMAIL_PATTERN)
    password: str = Field(..., min_length=1)


class EmailSchema(BaseModel):
    """Single-field payload for operations that only need an e-mail.

    Used by verification-email requests and password-reset-initiation
    endpoints where the user proves account ownership via e-mail.
    """

    email: str = Field(..., pattern=_EMAIL_PATTERN)


class ResetPasswordSchema(BaseModel):
    """Payload for ``POST /auth/reset-password``.

    Combines the one-time reset token with the new password and its
    confirmation. The token is validated server-side against the
    hashed value stored in the database.

    Attributes:
        email: The account whose password is being reset.
        token: One-time reset token received via e-mail.
        password: New plaintext password.
        confirm_password: Must equal ``password``.
    """

    email: str = Field(..., pattern=_EMAIL_PATTERN)
    token: str = Field(...)
    password: str = Field(..., min_length=8, max_length=128)
    confirm_password: str = Field(..., min_length=8, max_length=128)

    @field_validator("confirm_password")
    @classmethod
    def passwords_match(cls, v: str, info) -> str:
        """Ensure password confirmation matches.

        Args:
            v: The confirmation password.
            info: Pydantic ``ValidationInfo`` with earlier field values.

        Returns:
            The confirmation password unchanged.

        Raises:
            ValueError: If passwords do not match.
        """
        if info.data.get("password") != v:
            raise ValueError("Passwords do not match")
        return v


class VerifyEmailSchema(BaseModel):
    """Payload for ``POST /auth/verify-email``.

    Attributes:
        email: The e-mail address to verify.
        token: One-time verification token received via e-mail.
    """

    email: str = Field(..., pattern=_EMAIL_PATTERN)
    token: str = Field(...)


class SessionExchangeSchema(BaseModel):
    """Payload for ``POST /auth/session/exchange``.

    Exchanges a one-time session token (e.g., from an OAuth callback)
    for a full JWT token pair.

    Attributes:
        token: One-time session token issued during OAuth flow.
    """

    token: str = Field(...)


class UserOut(BaseModel):
    """Public user representation returned in API responses.

    Exposes only non-sensitive fields safe for client consumption.

    Attributes:
        id: Auto-incrementing user identifier.
        email: Registered e-mail address.
        auth_provider: How the user authenticates (``email`` or ``google``).
        is_email_verified: Whether the e-mail address has been confirmed.
        created_at: ISO-8601 timestamp of account creation, if available.
    """

    id: int
    email: str
    auth_provider: str
    is_email_verified: bool
    created_at: Optional[str] = None
