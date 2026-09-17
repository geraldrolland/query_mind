"""Pydantic schemas for DSL query plan validation.

These models define the complete structure of a DSL query plan as
produced by the LLM and validated before compilation. The top-level
``DslDefinitionSchema`` enforces cross-field invariants (e.g.,
grouped fields must appear in ``select``, metrics cannot overlap
with direct selects) and is consumed by the SQL compiler in
``dsl/postgresjsonBcompiler.py``.
"""

from pydantic import BaseModel, Field, model_validator
from typing import Literal, Union, Optional


class FilterSchema(BaseModel):
    """A single filter predicate applied to a dataset column.

    Attributes:
        field: Column name to filter on (non-empty).
        operator: Comparison operator. Supported values:
            ``eq``, ``neq``, ``gt``, ``gte``, ``lt``, ``lte``,
            ``between``, ``in``, ``nin``, ``contains``,
            ``before``, ``after``.
        value: Comparison operand. The type must be compatible with
            the operator (e.g., ``list`` for ``in``/``nin``,
            ``str`` for ``contains``).
    """

    field: str = Field(..., min_length=1)
    operator: Literal[
        "eq", "neq", "gt", "gte", "lt", "lte",
        "between", "in", "nin", "contains", "before", "after",
    ]
    value: Union[str, int, float, bool, list]


class SortSchema(BaseModel):
    """A single sort specification applied to query results.

    Attributes:
        field: Column name to sort by (non-empty).
        direction: Sort order — ``asc`` for ascending, ``desc`` for
            descending.
    """

    field: str = Field(..., min_length=1)
    direction: Literal["asc", "desc"]


class MeasurableSchema(BaseModel):
    """An aggregate metric computed over the dataset.

    ``count`` does not require a ``field`` (it counts rows), while all
    other metric types require exactly one ``field``. The ``alias``
    must differ from the ``field`` to avoid column-name collisions in
    the output.

    Attributes:
        metric_type: Aggregate function — ``sum``, ``count``, ``min``,
            ``max``, or ``avg``.
        field: Column to aggregate over; required for all metric types
            except ``count``.
        alias: Output column name for the computed metric.
    """

    metric_type: Literal["sum", "count", "min", "max", "avg"]
    field: Optional[str] = Field(default=None, min_length=1)
    alias: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def validate_metric(self):
        """Enforce field-presence rules and alias uniqueness.

        Returns:
            The validated ``MeasurableSchema`` instance.

        Raises:
            ValueError: If ``field`` is missing for non-count metrics,
                ``field`` is provided for count, or alias equals field.
        """
        if self.metric_type in ["sum", "min", "max", "avg"] and not self.field:
            raise ValueError(f"Field must be provided for metric type '{self.metric_type}'")
        if self.metric_type == "count" and self.field:
            raise ValueError("Field should not be provided for 'count' metric type")
        if self.field == self.alias:
            raise ValueError("Alias cannot be the same as the field name")
        return self


class DslDefinitionSchema(BaseModel):
    """Complete DSL query plan submitted by the LLM for compilation.

    This is the top-level schema that the DSL compiler validates and
    transforms into a PostgreSQL query. At least one of ``select`` or
    ``metrics`` must be provided; when both are present, the metric
    fields must not overlap with the select fields, and all grouped
    fields must appear in the select list.

    Attributes:
        select: Column names to include in the output. An empty list
            means "no raw columns" (metrics-only query).
        filters: List of filter predicates applied before grouping.
        sorts: List of sort specifications applied after grouping.
        group_by: Column names to group results by.
        metrics: Aggregate metric definitions to compute.
        limit: Maximum number of rows to return (1-1000); ``None``
            for no limit.
    """

    model_config = {"extra": "forbid"}

    select: list[str] = Field(default=[])
    filters: list[FilterSchema] = Field(default=[])
    sorts: list[SortSchema] = Field(default=[])
    group_by: list[str] = Field(default=[])
    metrics: list[MeasurableSchema] = Field(default=[])
    limit: Optional[int] = Field(default=None, ge=1, le=1000)

    @model_validator(mode="after")
    def validate_dsl(self):
        """Enforce cross-field invariants on the query plan.

        Returns:
            The validated ``DslDefinitionSchema`` instance.

        Raises:
            ValueError: If no columns or metrics are specified, metric
                fields overlap with selects, or grouped fields are
                missing from the select list.
        """
        if self.select == [] and not self.metrics:
            raise ValueError("At least one of 'select' or 'metrics' must be provided.")
        if self.select and self.metrics and any(m.field in self.select for m in self.metrics if m.field):
            raise ValueError("Fields used in 'metrics' cannot be included in 'select'.")
        if self.select and self.group_by and any(col not in self.select for col in self.group_by):
            raise ValueError("All fields in 'group_by' must be included in 'select'.")
        return self
