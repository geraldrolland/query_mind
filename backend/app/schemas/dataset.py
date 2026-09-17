"""Pydantic schemas for dataset upload, inspection, and chat request payloads.

These models drive the OpenAPI documentation and input validation for
all dataset-related endpoints: file upload, row retrieval, schema
inspection, and chat-request submission.
"""

from pydantic import BaseModel, Field
from typing import Optional, Literal


class CleaningReportSchema(BaseModel):
    """Summary of data-quality checks applied during CSV ingestion.

    Returned alongside the dataset metadata after a successful upload
    so the client can present a quick quality overview.

    Attributes:
        raw_rows: Number of rows in the original CSV file.
        rows_ingested: Rows remaining after cleaning (duplicates and
            empty rows removed).
        duplicates_removed: Count of exact-duplicate rows dropped.
        null_counts: Map of column name to the number of null values
            found in that column across all ingested rows.
        columns: Map of column name to its inferred PostgreSQL type
            (e.g. ``integer``, ``text``, ``timestamp``).
    """

    raw_rows: int
    rows_ingested: int
    duplicates_removed: int
    null_counts: dict[str, int]
    columns: dict[str, str]


class DatasetOut(BaseModel):
    """Compact dataset representation for list and detail responses.

    Attributes:
        id: Prefixed UUID primary key (``dst_<uuid>``).
        name: User-supplied dataset name.
        description: Optional free-text description.
        populated: ``True`` once the CSV has been fully ingested.
        total_rows: Number of data rows after cleaning.
        total_size_bytes: On-disk size of the ingested data.
        created_at: ISO-8601 creation timestamp.
        updated_at: ISO-8601 last-modified timestamp.
    """

    id: str
    name: str
    description: Optional[str] = None
    populated: bool
    total_rows: int
    total_size_bytes: int
    created_at: str
    updated_at: str


class UploadResponseSchema(BaseModel):
    """Response payload for ``POST /datasets/upload``.

    Wraps both the dataset metadata and the cleaning report so the
    client gets a single atomic response after upload.

    Attributes:
        dataset: The newly created dataset metadata.
        cleaning_report: Data-quality summary from the ingestion pipeline.
    """

    dataset: DatasetOut
    cleaning_report: CleaningReportSchema


class DatasetRowOut(BaseModel):
    """Single data row within a paginated records response.

    Attributes:
        id: Prefixed UUID primary key (``drow_<uuid>``).
        data: JSONB column values as a dictionary.
        created_at: ISO-8601 row-creation timestamp.
    """

    id: str
    data: dict
    created_at: str


class RecordsResponseSchema(BaseModel):
    """Paginated list of dataset rows.

    Attributes:
        records: List of row objects for the current page.
        total: Total number of rows across all pages.
        page: Current page number (1-indexed).
        page_size: Maximum rows per page.
    """

    records: list[DatasetRowOut]
    total: int
    page: int
    page_size: int


class IncomingMessage(BaseModel):
    """A single chat message submitted by the user.

    The ``status`` field is always ``pending`` on submission; the
    backend transitions it through ``queued`` -> ``sent`` as the
    LLM processes the request.

    Attributes:
        id: Client-generated message identifier.
        role: Must be ``user`` for incoming messages.
        status: Lifecycle state; always ``pending`` on submission.
        content: The user's natural-language query (1-4000 chars).
    """

    id: str
    role: Literal["user"]
    status: Literal["pending"]
    content: str = Field(max_length=4000, min_length=1)


class ChatRequestSchema(BaseModel):
    """Top-level payload for ``POST /chat/{dataset_id}/message``.

    Wraps the incoming message together with optional conversation
    history so the LLM can maintain context across turns.

    Attributes:
        message: The user's new chat message.
        history: Prior conversation turns (list of message dicts),
            used to maintain multi-turn context.
    """

    message: IncomingMessage
    history: Optional[list[dict]] = Field(default=[])
