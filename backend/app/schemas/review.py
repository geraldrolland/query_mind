"""Pydantic schemas for the review endpoint."""

from pydantic import BaseModel, Field


class ReviewSchema(BaseModel):
    """Request body for creating a review."""

    comment: str = Field(min_length=1, max_length=2000)
