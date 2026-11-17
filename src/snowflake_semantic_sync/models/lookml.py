"""Pydantic models representing LookML Analytic Model views (sql_analytic_model_name) and Explores."""

from typing import List, Optional, Set
from pydantic import BaseModel, Field, field_validator

# Supported LookML field types for sql_analytic_model_name views
ALLOWED_ANALYTIC_MODEL_FIELD_TYPES: Set[str] = {
    "string",
    "number",
    "date",
    "time",
    "date_time",
    "datetime",
    "yesno",
}

# Standalone dimensions and measures cannot use 'time' (Looker requires dimension_group for type: time)
ALLOWED_ANALYTIC_MODEL_DIMENSION_TYPES: Set[str] = ALLOWED_ANALYTIC_MODEL_FIELD_TYPES - {"time"}


class LookMLDimension(BaseModel):
    """Represents a LookML dimension inside a sql_analytic_model_name view."""
    name: str
    type: str = "string"
    sql: str
    primary_key: bool = False
    hidden: bool = False
    label: Optional[str] = None
    description: Optional[str] = None
    synonyms: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    suggestions: List[str] = Field(default_factory=list)
    value_format_name: Optional[str] = None
    group_label: Optional[str] = None

    @field_validator("type")
    @classmethod
    def validate_analytic_model_type(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized == "time":
            raise ValueError(
                "LookML dimension type 'time' may only be used in a dimension_group, not a standalone dimension "
                "(map Snowflake TIME columns to 'string')."
            )
        if normalized not in ALLOWED_ANALYTIC_MODEL_DIMENSION_TYPES:
            raise ValueError(
                f"Unsupported LookML field type '{v}' for sql_analytic_model_name view. "
                f"Allowed dimension types: {sorted(ALLOWED_ANALYTIC_MODEL_DIMENSION_TYPES)}"
            )
        return normalized


class LookMLDimensionGroup(BaseModel):
    """Represents a LookML dimension_group (type: time) inside a sql_analytic_model_name view."""
    name: str
    type: str = "time"
    sql: str
    timeframes: List[str] = Field(
        default_factory=lambda: ["raw", "time", "date", "week", "month", "quarter", "year"]
    )
    datatype: Optional[str] = None  # "date" | "timestamp" | "datetime"
    convert_tz: Optional[bool] = None
    hidden: bool = False
    label: Optional[str] = None
    description: Optional[str] = None
    synonyms: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    group_label: Optional[str] = None

    @field_validator("type")
    @classmethod
    def validate_dimension_group_type(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized != "time":
            raise ValueError(
                f"Unsupported LookML dimension_group type '{v}' for sql_analytic_model_name view. "
                f"Only 'time' is supported (e.g. 'duration' is rejected by Looker analytic models)."
            )
        return normalized


class LookMLMeasure(BaseModel):
    """Represents a LookML measure inside a sql_analytic_model_name view (compiled to AGG() by Looker)."""
    name: str
    type: str = "number"
    sql: str
    hidden: bool = False
    label: Optional[str] = None
    description: Optional[str] = None
    synonyms: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    value_format_name: Optional[str] = None
    group_label: Optional[str] = None

    @field_validator("type")
    @classmethod
    def validate_measure_type(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized == "time" or normalized not in ALLOWED_ANALYTIC_MODEL_DIMENSION_TYPES:
            raise ValueError(
                f"Unsupported LookML measure type '{v}' for sql_analytic_model_name view. "
                f"Measures on analytic models must use 'number' (or one of {sorted(ALLOWED_ANALYTIC_MODEL_DIMENSION_TYPES)})."
            )
        return normalized


class LookMLView(BaseModel):
    """Represents a LookML Analytic Model view using sql_analytic_model_name."""
    view_name: str
    base_view_name: str
    sql_analytic_model_name: str
    fields_hidden_by_default: bool = False
    description: Optional[str] = None
    dimension_groups: List[LookMLDimensionGroup] = Field(default_factory=list)
    dimensions: List[LookMLDimension] = Field(default_factory=list)
    measures: List[LookMLMeasure] = Field(default_factory=list)
    source_semantic_view: str = ""


class LookMLExplore(BaseModel):
    """Represents a LookML Explore backed by an Analytic Model view (no joins or aggregate_tables allowed)."""
    name: str
    view_name: str
    label: Optional[str] = None
    description: Optional[str] = None
    source_semantic_view: str = ""
