"""Pydantic configuration models for Declarative Semantic Mapping Profiles."""

from typing import Dict, List
from pydantic import BaseModel, Field


class ValueFormatRule(BaseModel):
    """Regex rule mapping field names to LookML value_format_name."""
    pattern: str
    value_format_name: str


class MappingProfile(BaseModel):
    """Declarative profile controlling how Snowflake Semantic Views map to LookML."""
    profile_name: str = "default_analytic_model"
    description: str = ""
    qualify_analytic_model_name: str = "database_schema"  # "database_schema" | "schema" | "unqualified"
    quote_identifiers: bool = False
    fields_hidden_by_default: bool = False
    include_facts: bool = False
    disambiguate_colliding_names: bool = True
    qualify_colliding_sql: bool = True
    use_dimension_groups: bool = True
    default_timeframes: List[str] = Field(
        default_factory=lambda: ["raw", "time", "date", "week", "month", "quarter", "year"]
    )
    date_timeframes: List[str] = Field(
        default_factory=lambda: ["raw", "date", "week", "month", "quarter", "year"]
    )
    generate_base_explores: bool = True
    type_mappings: Dict[str, str] = Field(
        default_factory=lambda: {
            "VARCHAR": "string",
            "TEXT": "string",
            "STRING": "string",
            "CHAR": "string",
            "CHARACTER": "string",
            "NVARCHAR": "string",
            "VARIANT": "string",
            "OBJECT": "string",
            "ARRAY": "string",
            "NUMBER": "number",
            "DECIMAL": "number",
            "NUMERIC": "number",
            "INT": "number",
            "INTEGER": "number",
            "BIGINT": "number",
            "SMALLINT": "number",
            "TINYINT": "number",
            "BYTEINT": "number",
            "FLOAT": "number",
            "FLOAT4": "number",
            "FLOAT8": "number",
            "DOUBLE": "number",
            "DOUBLE PRECISION": "number",
            "REAL": "number",
            "FIXED": "number",
            "BOOLEAN": "yesno",
            "BOOL": "yesno",
            "DATE": "date",
            "DATETIME": "date_time",
            "TIMESTAMP": "date_time",
            "TIMESTAMP_NTZ": "date_time",
            "TIMESTAMP_LTZ": "date_time",
            "TIMESTAMP_TZ": "date_time",
            "TIME": "time",
        }
    )
    measure_value_format_rules: List[ValueFormatRule] = Field(default_factory=list)
