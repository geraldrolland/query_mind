"""CSV cleaning: parse, drop duplicates, count nulls, infer types."""

import io
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

TYPE_MAP = {
    "int64": "number",
    "float64": "number",
    "object": "string",
    "bool": "boolean",
    "datetime64[ns]": "date",
    "datetime64[us]": "date",
}

OPERATORS_BY_TYPE = {
    "number": ["eq", "neq", "gt", "gte", "lt", "lte", "between", "in", "nin"],
    "string": ["eq", "neq", "contains", "in", "nin", "before", "after"],
    "boolean": ["eq", "neq"],
    "date": ["eq", "neq", "gt", "gte", "lt", "lte", "before", "after", "between", "in", "nin"],
}


@dataclass
class CleaningResult:
    """Outcome of a :func:`clean_csv` run.

    Attributes:
        raw_rows: Row count as parsed from the CSV, before deduplication.
        rows_ingested: Rows kept after dropping exact duplicates.
        duplicates_removed: ``raw_rows - rows_ingested``.
        null_counts: Missing-value count per column name.
        columns: Inferred logical type per column
            (``number``/``string``/``boolean``/``date``).
        schema: Full column rules incl. allowed operators and aggregability,
            ready to be stored on the dataset.
        rows: Cleaned rows as JSON-safe dicts (NaN replaced with None,
            integral floats normalized to ints, dates ISO-formatted).
    """

    raw_rows: int
    rows_ingested: int
    duplicates_removed: int
    null_counts: dict[str, int] = field(default_factory=dict)
    columns: dict[str, str] = field(default_factory=dict)
    schema: dict = field(default_factory=dict)
    rows: list[dict] = field(default_factory=list)


def _infer_type(series: pd.Series) -> str:
    """Infer the logical column type for a pandas Series.

    Classification order:
        1. All-null series default to ``string`` (no evidence either way).
        2. Native dtypes map directly (bool → ``boolean``, numeric dtype →
           ``number``, datetime dtype → ``date``).
        3. Object columns are coerced: when at least 3 non-null values exist
           and ≥80% parse as numbers or ISO dates, that type wins.

    Args:
        series: Column values to inspect.

    Returns:
        One of ``number``, ``string``, ``boolean``, ``date``.
    """
    if series.notna().sum() == 0:
        # All-null column carries no type evidence (read_csv yields float64
        # NaN for these) -> default to string rather than number.
        return "string"
    if series.dtype == "bool":
        return "boolean"
    if pd.api.types.is_numeric_dtype(series):
        return "number"
    if pd.api.types.is_datetime64_any_dtype(series):
        return "date"
    # Object columns: try numeric coercion on non-null values.
    sample = series.dropna()
    if len(sample) >= 3:
        coerced = pd.to_numeric(sample, errors="coerce")
        if coerced.notna().mean() >= 0.8:
            return "number"
        parsed = pd.to_datetime(sample.astype(str), errors="coerce", format="mixed")
        if parsed.notna().mean() >= 0.8:
            return "date"
    return "string"


def _normalize_float(value):
    """Collapse integral floats to ints for cleaner JSON output.

    Args:
        value: Any cell value.

    Returns:
        ``int(value)`` when the input is a float with no fractional part
        (e.g. ``980.0`` → ``980``); otherwise the value unchanged.
    """
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _parse_date_part(value, part: str) -> Optional[int]:
    """Extract a year/month/day integer from a date value (ISO string or None)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value)[:10])
        except ValueError:
            return None
    if part == "year":
        return dt.year
    if part == "month":
        return dt.month
    return dt.day


def enrich_dataset(rows: list[dict], schema: dict) -> None:
    """Add <date_column>_day/_month/_year number columns for every date column.

    The derived columns are typed as numbers (so they support the numeric
    operators and plain grouping) but are marked non-aggregable, so metrics
    such as sum/avg can never be applied to them.
    """
    date_columns = [col for col, rule in schema.items() if rule.get("type") == "date"]
    for col in date_columns:
        for part in ("year", "month", "day"):
            new_col = f"{col}_{part}"
            if new_col in schema:
                continue
            schema[new_col] = {
                "type": "number",
                "allowed_operators": OPERATORS_BY_TYPE["number"],
                "aggregable": False,
            }
            for row in rows:
                row[new_col] = _parse_date_part(row.get(col), part)


def clean_csv(file_bytes: bytes, filename: str = "upload.csv") -> CleaningResult:
    """Parse a CSV file, drop exact-duplicate rows, and infer the schema.

    Returns cleaned rows (as plain JSON-safe dicts) plus statistics used to
    build the cleaning report.
    """
    try:
        df = pd.read_csv(io.BytesIO(file_bytes))
    except Exception as exc:
        raise ValueError(f"Failed to parse CSV: {exc}") from exc

    if df.empty:
        raise ValueError("CSV file contains no rows")

    raw_rows = len(df)

    # Normalize column names (strip whitespace) and reject empties.
    df.columns = [str(c).strip() for c in df.columns]
    if any(not c for c in df.columns):
        raise ValueError("CSV contains a column with an empty name")

    # Treat empty/whitespace strings as missing values so null counts and
    # type inference behave consistently.
    df = df.replace(r"^\s*$", pd.NA, regex=True)

    # Drop exact-duplicate rows (all columns identical).
    df = df.drop_duplicates()
    duplicates_removed = raw_rows - len(df)

    # Null counts per column.
    null_counts = {col: int(df[col].isna().sum()) for col in df.columns}

    # Infer schema + types BEFORE the object/None conversion below, so native
    # pandas dtypes (bool, int64, float64, datetime64) drive classification;
    # otherwise bool columns get coerced to numbers by pd.to_numeric.
    columns: dict[str, str] = {}
    schema: dict = {}
    for col in df.columns:
        dtype = _infer_type(df[col])
        columns[col] = dtype
        schema[col] = {
            "type": dtype,
            "allowed_operators": OPERATORS_BY_TYPE[dtype],
            "aggregable": True if dtype == "number" else False,
        }

    # Replace NaN with None for JSON serialization (convert to object dtype first
    # so pandas does not coerce None back to NaN).
    df = df.astype(object).where(pd.notnull(df), None)

    rows: list[dict] = []
    for record in df.to_dict(orient="records"):
        clean = {}
        for key, value in record.items():
            if isinstance(value, (pd.Timestamp,)):
                value = value.isoformat()
            elif value is None:
                pass
            else:
                value = _normalize_float(value)
            clean[key] = value
        rows.append(clean)

    enrich_dataset(rows, schema)

    return CleaningResult(
        raw_rows=raw_rows,
        rows_ingested=len(rows),
        duplicates_removed=duplicates_removed,
        null_counts=null_counts,
        columns=columns,
        schema=schema,
        rows=rows,
    )
