"""Semantic Mapper translating SnowflakeSemanticView AST into LookML Analytic Model views and explores."""

from collections import Counter
import re
from typing import List, Optional

from snowflake_semantic_sync.identifiers import (
    canonical_snowflake_identifier,
    format_sql_identifier,
    unquote_snowflake_identifier,
)
from snowflake_semantic_sync.mapping.profile import MappingProfile
from snowflake_semantic_sync.models.lookml import (
    ALLOWED_ANALYTIC_MODEL_DIMENSION_TYPES,
    LookMLDimension,
    LookMLDimensionGroup,
    LookMLExplore,
    LookMLMeasure,
    LookMLView,
)
from snowflake_semantic_sync.models.snowflake import (
    SnowflakeSemanticField,
    SnowflakeSemanticView,
)


def to_snake_case(name: str) -> str:
    """Converts a Snowflake identifier or CamelCase string into clean LookML snake_case."""
    try:
        cleaned, _ = unquote_snowflake_identifier(name)
    except ValueError:
        cleaned = name.strip().strip('"')
    # Handle camelCase -> snake_case transitions if mixed case
    if any(c.islower() for c in cleaned) and any(c.isupper() for c in cleaned):
        cleaned = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", cleaned)
    cleaned = re.sub(r"[^a-zA-Z0-9_]+", "_", cleaned).lower().strip("_")
    cleaned = re.sub(r"_+", "_", cleaned)
    return cleaned or "unnamed_field"


def to_title_label(name: str) -> str:
    """Converts an identifier into a human-friendly Title Case label."""
    snake = to_snake_case(name)
    acronyms = {"id": "ID", "usd": "USD", "ytd": "YTD", "mtd": "MTD", "qtd": "QTD", "kpi": "KPI", "arr": "ARR", "mrr": "MRR"}
    words = [acronyms.get(w, w.capitalize()) for w in snake.split("_") if w]
    return " ".join(words)


def normalize_snowflake_base_type(raw_type: str) -> str:
    """Strips parenthesized precision/scale (e.g., 'NUMBER(38,2)' -> 'NUMBER') and normalizes whitespace."""
    if not raw_type:
        return "VARCHAR"
    base = re.sub(r"\(.*\)", "", raw_type).strip().upper()
    base = re.sub(r"\s+", " ", base)
    return base or "VARCHAR"


class SemanticModelMapper:
    """Maps a SnowflakeSemanticView to a LookMLView (sql_analytic_model_name) and LookMLExplore."""

    def __init__(self, profile: MappingProfile):
        self.profile = profile

    def _format_sql_analytic_model_name(self, sv: SnowflakeSemanticView) -> str:
        force_quote = bool(self.profile.quote_identifiers)
        mode = (self.profile.qualify_analytic_model_name or "database_schema").lower()
        if mode == "database_schema" and sv.database and sv.schema_name:
            return (
                f"{format_sql_identifier(sv.database, force_quote=force_quote)}"
                f".{format_sql_identifier(sv.schema_name, force_quote=force_quote)}"
                f".{format_sql_identifier(sv.name, force_quote=force_quote)}"
            )
        if mode == "schema" and sv.schema_name:
            return (
                f"{format_sql_identifier(sv.schema_name, force_quote=force_quote)}"
                f".{format_sql_identifier(sv.name, force_quote=force_quote)}"
            )
        return format_sql_identifier(sv.name, force_quote=force_quote)

    def _map_data_type(self, raw_type: str, kind: str) -> str:
        base = normalize_snowflake_base_type(raw_type)
        if kind == "time_dimension":
            if base == "DATE":
                return "date"
            if base == "TIME":
                return "string"
            return "date_time"

        mapped = self.profile.type_mappings.get(base, "string").lower()
        if mapped not in ALLOWED_ANALYTIC_MODEL_DIMENSION_TYPES:
            return "string"
        return mapped

    def _infer_measure_value_format(self, field_name: str) -> Optional[str]:
        snake = to_snake_case(field_name)
        for rule in self.profile.measure_value_format_rules:
            if re.search(rule.pattern, snake, flags=re.IGNORECASE):
                return rule.value_format_name
        return None

    def _is_dimension_group_candidate(self, field: SnowflakeSemanticField, lkml_type: str) -> bool:
        """Returns True if a temporal field should be emitted as a LookML dimension_group (type: time)."""
        if not self.profile.use_dimension_groups or field.kind == "fact":
            return False
        return lkml_type in ("date", "date_time", "datetime")

    def _choose_dimension_group_name(
        self,
        candidate_name: str,
        timeframes: List[str],
        reserved_names: set[str],
    ) -> str:
        """Strips redundant trailing '_date' or '_time' suffix when safe from field collisions."""
        stem = candidate_name
        for suffix in ("_date", "_time"):
            if candidate_name.endswith(suffix) and len(candidate_name) > len(suffix):
                proposed = candidate_name[: -len(suffix)]
                expanded = {f"{proposed}_{tf}" for tf in timeframes}
                if proposed not in reserved_names and not (expanded & reserved_names):
                    stem = proposed
                break
        return stem

    def _ensure_unique_lookml_name(
        self,
        base_name: str,
        used_names: set[str],
        suffix_hint: Optional[str] = None,
    ) -> str:
        """Guarantees a deterministic unique LookML identifier without silently dropping fields."""
        if base_name not in used_names:
            return base_name
        if suffix_hint:
            hinted = f"{base_name}_{suffix_hint}"
            if hinted not in used_names:
                return hinted
        idx = 2
        while f"{base_name}_{idx}" in used_names:
            idx += 1
        return f"{base_name}_{idx}"

    def _ensure_unique_dimension_group_name(
        self,
        candidate_name: str,
        timeframes: List[str],
        used_names: set[str],
        other_reserved: set[str],
    ) -> str:
        """Chooses a collision-free dimension_group name considering both stem and expanded timeframe names."""
        blocked = used_names | other_reserved
        stem = self._choose_dimension_group_name(candidate_name, timeframes, blocked)
        stem_expanded = {f"{stem}_{tf}" for tf in timeframes}
        if stem not in used_names and not (stem_expanded & used_names):
            return stem
        idx = 2
        while True:
            alt = f"{candidate_name}_{idx}"
            alt_expanded = {f"{alt}_{tf}" for tf in timeframes}
            if alt not in blocked and not (alt_expanded & blocked):
                return alt
            idx += 1

    def _format_field_sql(
        self,
        field: SnowflakeSemanticField,
        canonical_name_counts: Counter,
    ) -> str:
        """Formats ${TABLE}.<FIELD> or ${TABLE}.<LOGICAL_TABLE>.<FIELD> when canonical SQL names collide."""
        sql_field = format_sql_identifier(field.name)
        canonical = canonical_snowflake_identifier(field.name)
        if (
            self.profile.qualify_colliding_sql
            and canonical_name_counts[canonical] > 1
            and field.logical_table
        ):
            sql_table = format_sql_identifier(field.logical_table)
            return f"${{TABLE}}.{sql_table}.{sql_field}"
        return f"${{TABLE}}.{sql_field}"

    def map_semantic_view_to_lookml_view(self, sv: SnowflakeSemanticView) -> LookMLView:
        """Converts a SnowflakeSemanticView into a LookMLView using sql_analytic_model_name."""
        view_name = to_snake_case(sv.name)
        sql_model_name = self._format_sql_analytic_model_name(sv)
        multi_table = len(sv.tables) > 1

        # Collect candidate dimension-like fields
        dim_candidates: List[SnowflakeSemanticField] = []
        metric_candidates: List[SnowflakeSemanticField] = []

        for table in sv.tables:
            dim_candidates.extend(table.dimensions)
            dim_candidates.extend(table.time_dimensions)
            if self.profile.include_facts:
                dim_candidates.extend(table.facts)
            metric_candidates.extend(table.metrics)

        metric_candidates.extend(sv.model_metrics)

        # Count occurrences of LookML snake_case names and Snowflake canonical SQL names to detect collisions
        all_candidates = dim_candidates + metric_candidates
        all_snake_counts = Counter(to_snake_case(f.name) for f in all_candidates)
        canonical_name_counts = Counter(canonical_snowflake_identifier(f.name) for f in all_candidates)

        def _initial_lkml_name(f: SnowflakeSemanticField) -> str:
            base_snake = to_snake_case(f.name)
            if (
                self.profile.disambiguate_colliding_names
                and all_snake_counts[base_snake] > 1
                and f.logical_table
            ):
                return f"{to_snake_case(f.logical_table)}_{base_snake}"
            return base_snake

        # Precompute ALL initial candidate names (including other dimension_groups) so suffix stripping never clobbers
        candidate_resolved_counts = Counter(_initial_lkml_name(f) for f in all_candidates)

        dimension_groups: List[LookMLDimensionGroup] = []
        dimensions: List[LookMLDimension] = []
        used_lkml_names: set[str] = set()

        for field in dim_candidates:
            lkml_name = _initial_lkml_name(field)
            lkml_type = self._map_data_type(field.data_type, field.kind)
            is_hidden_fact = field.kind == "fact"
            group_label = to_title_label(field.logical_table) if (multi_table and field.logical_table) else None
            sql_expr = self._format_field_sql(field, canonical_name_counts)

            if self._is_dimension_group_candidate(field, lkml_type):
                sf_base_type = normalize_snowflake_base_type(field.data_type)
                if sf_base_type == "DATE" or lkml_type == "date":
                    timeframes = list(self.profile.date_timeframes)
                    datatype = "date"
                    convert_tz: Optional[bool] = False
                else:
                    timeframes = list(self.profile.default_timeframes)
                    datatype = "timestamp"
                    # Naive wall-clock timestamps (TIMESTAMP_NTZ / DATETIME) have no TZ offset; disable convert_tz
                    convert_tz = False if sf_base_type in ("TIMESTAMP_NTZ", "DATETIME") else None

                other_reserved = {
                    name
                    for name, cnt in candidate_resolved_counts.items()
                    if name != lkml_name or cnt > 1
                }
                dg_name = self._ensure_unique_dimension_group_name(
                    lkml_name,
                    timeframes,
                    used_lkml_names,
                    other_reserved,
                )
                used_lkml_names.add(dg_name)
                for tf in timeframes:
                    used_lkml_names.add(f"{dg_name}_{tf}")

                dimension_groups.append(
                    LookMLDimensionGroup(
                        name=dg_name,
                        type="time",
                        sql=sql_expr,
                        timeframes=timeframes,
                        datatype=datatype,
                        convert_tz=convert_tz,
                        hidden=False,
                        description=field.description,
                        synonyms=list(field.synonyms),
                        group_label=group_label,
                    )
                )
                continue

            unique_dim_name = self._ensure_unique_lookml_name(lkml_name, used_lkml_names)
            used_lkml_names.add(unique_dim_name)

            dimensions.append(
                LookMLDimension(
                    name=unique_dim_name,
                    type=lkml_type,
                    sql=sql_expr,
                    primary_key=False,
                    hidden=is_hidden_fact,
                    description=field.description,
                    synonyms=list(field.synonyms),
                    suggestions=list(field.sample_values[:25]) if lkml_type == "string" and field.sample_values else [],
                    group_label=group_label,
                )
            )

        measures: List[LookMLMeasure] = []

        for field in metric_candidates:
            base_snake = to_snake_case(field.name)
            if (
                self.profile.disambiguate_colliding_names
                and (all_snake_counts[base_snake] > 1 or base_snake in used_lkml_names)
                and field.logical_table
            ):
                candidate_measure_name = f"{to_snake_case(field.logical_table)}_{base_snake}"
            else:
                candidate_measure_name = base_snake

            unique_measure_name = self._ensure_unique_lookml_name(
                candidate_measure_name,
                used_lkml_names,
                suffix_hint="metric",
            )
            used_lkml_names.add(unique_measure_name)

            group_label = to_title_label(field.logical_table) if (multi_table and field.logical_table) else None
            val_fmt = self._infer_measure_value_format(unique_measure_name)
            sql_expr = self._format_field_sql(field, canonical_name_counts)

            measures.append(
                LookMLMeasure(
                    name=unique_measure_name,
                    type="number",
                    sql=sql_expr,
                    hidden=False,
                    description=field.description,
                    synonyms=list(field.synonyms),
                    value_format_name=val_fmt,
                    group_label=group_label,
                )
            )

        # Deterministic alphabetical ordering by name for stable diffs
        dimension_groups.sort(key=lambda dg: dg.name)
        dimensions.sort(key=lambda d: d.name)
        measures.sort(key=lambda m: m.name)

        return LookMLView(
            view_name=view_name,
            base_view_name=view_name,
            sql_analytic_model_name=sql_model_name,
            fields_hidden_by_default=self.profile.fields_hidden_by_default,
            description=sv.description,
            dimension_groups=dimension_groups,
            dimensions=dimensions,
            measures=measures,
            source_semantic_view=sv.fully_qualified_name,
        )

    def map_semantic_view_to_explore(self, sv: SnowflakeSemanticView, view: LookMLView) -> LookMLExplore:
        """Creates a LookMLExplore for the Analytic Model view."""
        return LookMLExplore(
            name=view.view_name,
            view_name=view.view_name,
            label=to_title_label(sv.name),
            description=sv.description,
            source_semantic_view=sv.fully_qualified_name,
        )
