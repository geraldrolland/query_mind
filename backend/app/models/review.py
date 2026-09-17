"""Review database model."""

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlmodel import Column, Field, SQLModel


def _generate_review_id() -> str:
    return f"rev_{uuid.uuid4().hex}"


class Review(SQLModel, table=True):
    """A user-submitted review stored in the database."""

    __tablename__ = "review"

    id: str = Field(
        default_factory=_generate_review_id,
        primary_key=True,
        max_length=36,
    )
    user_id: int = Field(
        sa_column=Column(
            sa.Integer,
            sa.ForeignKey("qm_user.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
    )
    comment: str = Field(max_length=2000)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
