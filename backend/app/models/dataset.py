"""SQLModel ORM definition for the ``dataset`` table.

The ``Dataset`` model is the central entity in QueryMind. It owns the
JSONB ``dataset_schema`` that the DSL compiler reads to resolve field
names, types, and constraints when translating natural-language queries
into SQL.
"""

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import Column, ForeignKey, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class Dataset(SQLModel, table=True):
    """Top-level dataset entity mapped to the ``dataset`` table.

    ``dataset_schema`` is required by the DSL compiler which fetches it
    via ``SELECT dataset_schema FROM dataset WHERE id = :dataset_id``.

    Attributes:
        id: Prefixed UUID primary key (``dst_<uuid>``).
        user_id: Foreign key to the owning user; cascade-deletes
            all associated rows and messages.
        name: Human-readable dataset name; indexed for list queries.
        description: Optional free-text description (max 500 chars).
        dataset_schema: JSONB dictionary describing column names,
            types, and constraints. This is the single source of
            truth consumed by the DSL compiler at query time.
        populated: ``True`` once the initial CSV upload has been
            ingested into ``datasetrow``. Gate-keeps query access.
        total_rows: Row count after ingestion; updated atomically
            during the cleaning pipeline.
        total_size_bytes: Sum of all ingested row byte sizes,
            used to enforce per-dataset quotas.
        created_at: UTC timestamp of dataset creation.
        updated_at: UTC timestamp of last modification; auto-
            refreshed by the ``onupdate`` server-side trigger.
    """

    __tablename__ = "dataset"

    id: str = Field(default_factory=lambda: f"dst_{uuid4()}", primary_key=True)
    user_id: int = Field(
        sa_column=Column(
            "user_id",
            ForeignKey("qm_user.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    name: str = Field(index=True)
    description: str | None = Field(default=None, max_length=500)
    dataset_schema: dict = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False))
    populated: bool = Field(default=False)
    total_rows: int = Field(default=0)
    total_size_bytes: int = Field(default=0)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        nullable=False,
        sa_column_kwargs={
            "server_default": func.now(),
            "onupdate": func.now(),
            "nullable": False,
        },
    )
