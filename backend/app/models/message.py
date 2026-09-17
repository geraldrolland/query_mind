"""SQLModel ORM definitions and enumerations for the ``message`` table.

Messages represent every turn in a chat conversation. Each message is
scoped to exactly one dataset and carries a JSONB ``content`` payload
whose structure depends on the ``type`` (``text`` vs ``dsl``). The
``chart_type`` field is used by the frontend renderer to choose the
appropriate visualization widget.
"""

from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

from sqlalchemy import Column, ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class MessageChartTypeEnum(str, Enum):
    """Type of visualization the frontend should render for this message.

    ``none`` indicates the message carries no chart and should be
    displayed as plain text only.
    """

    none = "none"
    barchart = "barchart"
    linechart = "linechart"
    tablechart = "tablechart"
    metricchart = "metricchart"
    piechart = "piechart"


class StatusEnum(str, Enum):
    """Lifecycle state of a message during the LLM response pipeline.

    Members:
        queued: Enqueued for the LLM worker; not yet processed.
        pending: Processing has started; a response is being streamed.
        sent: The complete response has been delivered to the client.
    """

    queued = "queued"
    pending = "pending"
    sent = "sent"


class MessageRoleEnum(str, Enum):
    """Originator of the message within a conversation turn.

    Members:
        user: Human-authored message.
        assistant: Machine-generated response from the LLM.
    """

    user = "user"
    assistant = "assistant"


class Message(SQLModel, table=True):
    """Chat message between a user and the assistant.

    Each message is scoped to a single dataset. The ``content`` column
    is stored as PostgreSQL JSONB to support both plain-text replies
    and structured DSL response payloads.

    Attributes:
        id: Prefixed UUID primary key (``msg_<uuid>``).
        dataset_id: Foreign key to the owning dataset.
        status: Current lifecycle state in the streaming pipeline.
        role: Whether the message was written by the ``user`` or the
            ``assistant``.
        type: Content type hint (e.g. ``text``, ``dsl``).
        chart_type: Visualization hint for the frontend renderer.
        is_error: ``True`` if this message represents an error
            response rather than a normal reply.
        content: Arbitrary JSONB payload whose shape is determined by
            ``type`` and ``chart_type``.
        created_at: UTC timestamp of message creation.
    """

    __tablename__ = "message"

    id: str = Field(default_factory=lambda: f"msg_{uuid4()}", primary_key=True)
    dataset_id: str = Field(
        sa_column=Column(
            "dataset_id",
            ForeignKey("dataset.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    status: str = Field(default=StatusEnum.sent, nullable=True, index=True)
    role: str = Field(default=MessageRoleEnum.user, index=True, nullable=False)
    type: str = Field(default="text", nullable=False)
    chart_type: str = Field(default=MessageChartTypeEnum.none, nullable=False)
    is_error: bool = Field(default=False, nullable=False)
    content: str | dict = Field(
        sa_column=Column(JSONB, nullable=False)
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc), nullable=False
    )
