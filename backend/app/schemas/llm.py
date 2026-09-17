"""Pydantic schemas for structured LLM response payloads.

These models define the discriminated union of content blocks that the
LLM may return (plain text or a DSL-backed data chart) along with the
top-level ``LLMResponse`` wrapper used for SSE streaming and direct
REST responses.
"""

from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field


class ChartTypeEnum(str, Enum):
    """Supported visualization types for DSL query results.

    Members:
        barchart: Vertical bar chart.
        linechart: Line chart for time-series data.
        tablechart: Tabular data display.
        metricchart: Single aggregated metric (KPI card).
        piechart: Pie chart for categorical proportions.
    """

    barchart = "barchart"
    linechart = "linechart"
    tablechart = "tablechart"
    metricchart = "metricchart"
    piechart = "piechart"


class TextContentBlock(BaseModel):
    """A plain-text content block within an LLM response.

    Attributes:
        type: Discriminator; always ``text`` for this block type.
        text: The rendered text content.
    """

    model_config = {"extra": "forbid"}

    type: Literal["text"]
    text: str


class RecordContentBlock(BaseModel):
    """A DSL-backed data chart content block within an LLM response.

    Attributes:
        type: Discriminator; always ``record`` for this block type.
        dsl_field: Compiled DSL query result or query metadata that
            the frontend uses to fetch and render the data.
        chart_type: Which visualization widget to render.
    """

    model_config = {"extra": "forbid"}

    type: Literal["record"]
    dsl_field: dict
    chart_type: ChartTypeEnum


# Discriminated union that the LLM response parser uses to deserialize
# each content block into the correct Pydantic model.
ContentBlock = Annotated[
    Union[TextContentBlock, RecordContentBlock], Field(discriminator="type")
]


class LLMResponse(BaseModel):
    """Top-level wrapper for an LLM response containing one or more blocks.

    The ``content`` list may contain any mix of ``TextContentBlock`` and
    ``RecordContentBlock`` entries, in the order the LLM produced them.

    Attributes:
        content: Ordered list of content blocks in this response.
    """

    model_config = {"extra": "forbid"}

    content: list[ContentBlock]
