"""Pydantic models representing a Snowflake Semantic View parsed from SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW."""

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
import yaml


def _normalize_synonyms(raw: Any) -> List[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str):
        cleaned = raw.strip()
        if not cleaned:
            return []
        return [part.strip() for part in cleaned.split(",") if part.strip()]
    return []


def _normalize_sample_values(raw: Any) -> List[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if item is not None and str(item).strip()]
    return []


class SnowflakeBaseTableRef(BaseModel):
    """Physical base table backing a logical table in a Snowflake Semantic View."""
    database: str = ""
    schema_name: str = Field(default="", alias="schema")
    table: str = ""

    model_config = {"populate_by_name": True}


class SnowflakeSemanticField(BaseModel):
    """Represents a dimension, time_dimension, fact, or metric in a Snowflake Semantic View."""
    name: str
    kind: str  # "dimension" | "time_dimension" | "fact" | "metric"
    logical_table: Optional[str] = None
    expr: str = ""
    data_type: str = "VARCHAR"
    description: Optional[str] = None
    synonyms: List[str] = Field(default_factory=list)
    sample_values: List[str] = Field(default_factory=list)
    unique: bool = False
    is_primary_key: bool = False
    access_modifier: str = "PUBLIC"


class SnowflakeLogicalTable(BaseModel):
    """Represents a logical table block inside a Snowflake Semantic View YAML."""
    name: str
    description: Optional[str] = None
    synonyms: List[str] = Field(default_factory=list)
    base_table: SnowflakeBaseTableRef = Field(default_factory=SnowflakeBaseTableRef)
    primary_key_columns: List[str] = Field(default_factory=list)
    dimensions: List[SnowflakeSemanticField] = Field(default_factory=list)
    time_dimensions: List[SnowflakeSemanticField] = Field(default_factory=list)
    facts: List[SnowflakeSemanticField] = Field(default_factory=list)
    metrics: List[SnowflakeSemanticField] = Field(default_factory=list)


class SnowflakeRelationshipColumn(BaseModel):
    """Column pair in a Snowflake Semantic View relationship."""
    left_column: str
    right_column: str


class SnowflakeRelationship(BaseModel):
    """Relationship between two logical tables in a Snowflake Semantic View."""
    name: str
    left_table: str
    right_table: str
    relationship_columns: List[SnowflakeRelationshipColumn] = Field(default_factory=list)
    join_type: Optional[str] = None
    relationship_type: Optional[str] = None


class SnowflakeSemanticView(BaseModel):
    """Normalized representation of a Snowflake Semantic View extracted via SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW."""
    name: str
    database: str
    schema_name: str
    description: Optional[str] = None
    tables: List[SnowflakeLogicalTable] = Field(default_factory=list)
    relationships: List[SnowflakeRelationship] = Field(default_factory=list)
    model_metrics: List[SnowflakeSemanticField] = Field(default_factory=list)
    raw_yaml: str = ""

    @property
    def fully_qualified_name(self) -> str:
        return f"{self.database}.{self.schema_name}.{self.name}"


def _parse_field_list(
    raw_items: Any,
    kind: str,
    logical_table: Optional[str] = None,
    pk_cols: Optional[set[str]] = None,
) -> List[SnowflakeSemanticField]:
    if not isinstance(raw_items, list):
        return []
    fields: List[SnowflakeSemanticField] = []
    pk_lookup = {c.lower() for c in (pk_cols or set())}
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        expr = str(item.get("expr") or name).strip()
        default_dt = "NUMBER" if kind in ("metric", "fact") else ("TIMESTAMP_NTZ" if kind == "time_dimension" else "VARCHAR")
        data_type = str(item.get("data_type") or default_dt).strip()
        description = item.get("description") or item.get("comment")
        desc_str = str(description).strip() if description else None
        access_mod = str(item.get("access_modifier") or "PUBLIC").strip().upper()
        # Skip private facts/metrics/dimensions that cannot be queried externally
        # Snowflake emits "private_access" (DDL YAML) or "PRIVATE" (Cortex YAML)
        if access_mod == "PRIVATE" or access_mod == "PRIVATE_ACCESS" or access_mod.startswith("PRIVATE"):
            continue
        is_pk = (name.lower() in pk_lookup) or (expr.lower() in pk_lookup)
        fields.append(
            SnowflakeSemanticField(
                name=name,
                kind=kind,
                logical_table=logical_table,
                expr=expr,
                data_type=data_type,
                description=desc_str,
                synonyms=_normalize_synonyms(item.get("synonyms")),
                sample_values=_normalize_sample_values(item.get("sample_values")),
                unique=bool(item.get("unique", False)),
                is_primary_key=is_pk,
                access_modifier=access_mod,
            )
        )
    return fields


def parse_semantic_view_yaml(
    raw_yaml: str,
    database: str,
    schema_name: str,
    fallback_name: Optional[str] = None,
) -> SnowflakeSemanticView:
    """Parses the YAML string returned by SELECT SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW('<db>.<schema>.<view>')."""
    if not raw_yaml or not raw_yaml.strip():
        raise ValueError("Empty YAML payload returned for Snowflake Semantic View.")

    payload = yaml.safe_load(raw_yaml)
    if not isinstance(payload, dict):
        raise ValueError("Invalid Snowflake Semantic View YAML: root element must be a mapping.")

    view_name = str(payload.get("name") or fallback_name or "").strip()
    if not view_name:
        raise ValueError("Snowflake Semantic View YAML is missing 'name'.")

    view_desc = payload.get("description") or payload.get("comment")
    view_desc_str = str(view_desc).strip() if view_desc else None

    logical_tables: List[SnowflakeLogicalTable] = []
    for raw_table in payload.get("tables") or []:
        if not isinstance(raw_table, dict):
            continue
        t_name = str(raw_table.get("name") or "").strip()
        if not t_name:
            continue
        t_desc = raw_table.get("description") or raw_table.get("comment")
        base_ref_raw = raw_table.get("base_table") or {}
        base_table = SnowflakeBaseTableRef(
            database=str(base_ref_raw.get("database") or database),
            schema=str(base_ref_raw.get("schema") or schema_name),
            table=str(base_ref_raw.get("table") or t_name),
        )

        pk_raw = raw_table.get("primary_key") or {}
        pk_cols = [
            str(c).strip()
            for c in (pk_raw.get("columns") if isinstance(pk_raw, dict) else []) or []
            if str(c).strip()
        ]
        pk_set = set(pk_cols)

        dims = _parse_field_list(raw_table.get("dimensions"), "dimension", logical_table=t_name, pk_cols=pk_set)
        time_dims = _parse_field_list(
            raw_table.get("time_dimensions"), "time_dimension", logical_table=t_name, pk_cols=pk_set
        )
        # Snowflake YAML uses 'facts' (or legacy 'measures' in older Cortex YAML) for row-level facts
        raw_facts = raw_table.get("facts") if "facts" in raw_table else raw_table.get("measures")
        facts = _parse_field_list(raw_facts, "fact", logical_table=t_name)
        metrics = _parse_field_list(raw_table.get("metrics"), "metric", logical_table=t_name)

        logical_tables.append(
            SnowflakeLogicalTable(
                name=t_name,
                description=str(t_desc).strip() if t_desc else None,
                synonyms=_normalize_synonyms(raw_table.get("synonyms")),
                base_table=base_table,
                primary_key_columns=pk_cols,
                dimensions=dims,
                time_dimensions=time_dims,
                facts=facts,
                metrics=metrics,
            )
        )

    relationships: List[SnowflakeRelationship] = []
    for raw_rel in payload.get("relationships") or []:
        if not isinstance(raw_rel, dict):
            continue
        rel_name = str(raw_rel.get("name") or "").strip()
        left_table = str(raw_rel.get("left_table") or "").strip()
        right_table = str(raw_rel.get("right_table") or "").strip()
        if not (left_table and right_table):
            continue
        rel_cols = [
            SnowflakeRelationshipColumn(
                left_column=str(rc.get("left_column") or "").strip(),
                right_column=str(rc.get("right_column") or "").strip(),
            )
            for rc in (raw_rel.get("relationship_columns") or [])
            if isinstance(rc, dict) and rc.get("left_column") and rc.get("right_column")
        ]
        relationships.append(
            SnowflakeRelationship(
                name=rel_name or f"{left_table}_to_{right_table}",
                left_table=left_table,
                right_table=right_table,
                relationship_columns=rel_cols,
                join_type=raw_rel.get("join_type"),
                relationship_type=raw_rel.get("relationship_type"),
            )
        )

    model_metrics = _parse_field_list(payload.get("metrics"), "metric", logical_table=None)

    return SnowflakeSemanticView(
        name=view_name,
        database=database,
        schema_name=schema_name,
        description=view_desc_str,
        tables=logical_tables,
        relationships=relationships,
        model_metrics=model_metrics,
        raw_yaml=raw_yaml,
    )
