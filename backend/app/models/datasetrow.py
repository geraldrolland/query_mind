"""SQLModel ORM definition for the ``datasetrow`` table.

Each ``DatasetRow`` is a single record within a dataset. The actual
column values are stored as a PostgreSQL JSONB document in the ``data``
column, allowing schema-flexible ingestion without ALTER TABLE
operations when columns change between uploads.
"""

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import Column, ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class DatasetRow(SQLModel, table=True):
    """One row of dataset data stored as a JSONB document.

    Table name and column names match what the ported DSL compiler
    emits (``FROM datasetrow``, ``data->>'field'``,
    ``datasetrow.dataset_id``).

    Attributes:
        id: Prefixed UUID primary key (``drow_<uuid>``).
        dataset_id: Foreign key to the parent dataset; cascade-deletes
            when the dataset is removed.
        data: Arbitrary JSONB dictionary whose keys correspond to
            dataset column names as defined in
            ``Dataset.dataset_schema``.
        created_at: UTC timestamp of row creation; set once during
            ingestion and never updated.
    """

    __tablename__ = "datasetrow"

    id: str = Field(default_factory=lambda: f"drow_{uuid4()}", primary_key=True)
    dataset_id: str = Field(
        sa_column=Column(
            "dataset_id",
            ForeignKey("dataset.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    data: dict = Field(sa_column=Column(JSONB, nullable=False))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc), nullable=False
    )
