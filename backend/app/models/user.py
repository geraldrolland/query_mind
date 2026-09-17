"""SQLModel ORM definitions for the ``qm_user`` table.

This module defines the canonical ``User`` model used across the
application. It backs both the authentication and dataset-ownership
workflows. Passwords are stored as bcrypt hashes; the plain-text
password never reaches the database.
"""

from datetime import datetime, timezone
from enum import Enum

from sqlmodel import Field, SQLModel


class AuthProviderEnum(str, Enum):
    """Discriminator for the authentication mechanism a user registered with.

    Members:
        email: Traditional e-mail + password registration.
        google: OAuth 2.0 via Google Identity Platform.
    """

    email = "email"
    google = "google"


class User(SQLModel, table=True):
    """Application user record mapped to the ``qm_user`` table.

    Attributes:
        id: Auto-incrementing primary key.
        email: Unique, indexed e-mail address (also the login username).
        hashed_password: Bcrypt hash of the password; ``None`` for
            OAuth-only accounts that have never set a local password.
        auth_provider: ``email`` or ``google`` — how the user first
            authenticated. Immutable after creation.
        is_email_verified: Set to ``True`` after the user clicks the
            verification link sent on registration.
        created_at: UTC timestamp of account creation; set once by the
            application layer.
    """

    __tablename__ = "qm_user"

    id: int = Field(primary_key=True)
    email: str = Field(index=True, unique=True)
    hashed_password: str | None = Field(default=None)
    auth_provider: str = Field(default="email")
    is_email_verified: bool = Field(default=False)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc), nullable=False
    )
