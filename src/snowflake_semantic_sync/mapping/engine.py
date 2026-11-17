"""Semantic Mapper translating SnowflakeSemanticView AST into LookML Analytic Model views and explores."""

from collections import Counter
import re
from typing import List, Optional

from snowflake_semantic_sync.mapping.profile import MappingProfile
from snowflake_semantic_sync.models.lookml import (
    ALLOWED_ANALYTIC_MODEL_FIELD_TYPES,
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
        def _q(ident: str) -> str:
            raw = ident.strip().strip('"')
            return f'"{raw}"' if self.profile.quote_identifiers else raw

        mode = (self.profile.qualify_analytic_model_name or "database_schema").lower()
        if mode == "database_schema" and sv.database and sv.schema_name:
            return f"{_q(sv.database)}.{_q(sv.schema_name)}.{_q(sv.name)}"
        if mode == "schema" and sv.schema_name:
            return f"{_q(sv.schema_name)}.{_q(sv.name)}"
        return _q(sv.name)

    def _map_data_type(self, raw_type: str, kind: str) -> str:
        if kind == "time_dimension":
            base = normalize_snowflake_base_type(raw_type)
            if base == "DATE":
                return "date"
            if base == "TIME":
                return "time"
            return "date_time"

        base = normalize_snowflake_base_type(raw_type)
        mapped = self.profile.type_mappings.get(base, "string").lower()
        if mapped not in ALLOWED_ANALYTIC_MODEL_FIELD_TYPES:
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

    def _format_field_sql(
        self,
        field: SnowflakeSemanticField,
        raw_name_counts: Counter,
    ) -> str:
        """Formats ${TABLE}.<FIELD> or ${TABLE}.<LOGICAL_TABLE>.<FIELD> when raw names collide across entities."""
        if (
            self.profile.qualify_colliding_sql
            and raw_name_counts[field.name.lower()] > 1
            and field.logical_table
        ):
            return f"${{TABLE}}.{field.logical_table}.{field.name}"
        return f"${{TABLE}}.{field.name}"

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

        # Count occurrences of snake_case and raw names across all fields to detect collisions
        all_candidates = dim_candidates + metric_candidates
        all_snake_counts = Counter(to_snake_case(f.name) for f in all_candidates)
        raw_name_counts = Counter(f.name.lower() for f in all_candidates)

        # Precompute non-temporal field names so dimension_group suffix stripping never collides
        reserved_names: set[str] = set()
        for f in all_candidates:
            base_snake = to_snake_case(f.name)
            if self.profile.disambiguate_colliding_names and all_snake_counts[base_snake] > 1 and f.logical_table:
                resolved = f"{to_snake_case(f.logical_table)}_{base_snake}"
            else:
                resolved = base_snake
            lkml_type = self._map_data_type(f.data_type, f.kind) if f.kind != "metric" else "number"
            if f.kind == "metric" or not self._is_dimension_group_candidate(f, lkml_type):
                reserved_names.add(resolved)

        dimension_groups: List[LookMLDimensionGroup] = []
        dimensions: List[LookMLDimension] = []
        seen_field_names: set[str] = set()

        for field in dim_candidates:
            base_snake = to_snake_case(field.name)
            if (
                self.profile.disambiguate_colliding_names
                and all_snake_counts[base_snake] > 1
                and field.logical_table
            ):
                lkml_name = f"{to_snake_case(field.logical_table)}_{base_snake}"
            else:
                lkml_name = base_snake

            lkml_type = self._map_data_type(field.data_type, field.kind)
            is_hidden_fact = field.kind == "fact"
            group_label = to_title_label(field.logical_table) if (multi_table and field.logical_table) else None
            sql_expr = self._format_field_sql(field, raw_name_counts)

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

                dg_name = self._choose_dimension_group_name(lkml_name, timeframes, reserved_names | seen_field_names)
                if dg_name in seen_field_names:
                    continue
                seen_field_names.add(dg_name)
                for tf in timeframes:
                    seen_field_names.add(f"{dg_name}_{tf}")

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

            if lkml_name in seen_field_names:
                continue
            seen_field_names.add(lkml_name)

            dimensions.append(
                LookMLDimension(
                    name=lkml_name,
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
        seen_measure_names: set[str] = set()

        for field in metric_candidates:
            base_snake = to_snake_case(field.name)
            if (
                self.profile.disambiguate_colliding_names
                and (all_snake_counts[base_snake] > 1 or base_snake in seen_field_names)
                and field.logical_table
            ):
                lkml_name = f"{to_snake_case(field.logical_table)}_{base_snake}"
            elif base_snake in seen_field_names:
                lkml_name = f"{base_snake}_metric"
            else:
                lkml_name = base_snake

            if lkml_name in seen_measure_names:
                continue
            seen_measure_names.add(lkml_name)

            group_label = to_title_label(field.logical_table) if (multi_table and field.logical_table) else None
            val_fmt = self._infer_measure_value_format(lkml_name)
            sql_expr = self._format_field_sql(field, raw_name_counts)

            measures.append(
                LookMLMeasure(
                    name=lkml_name,
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
