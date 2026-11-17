"""Unit tests for SemanticModelMapper and LookMLGenerator (sql_analytic_model_name)."""

from pathlib import Path
import lkml
import pytest

from snowflake_semantic_sync.generator.engine import LookMLGenerator
from snowflake_semantic_sync.mapping.engine import SemanticModelMapper
from snowflake_semantic_sync.mapping.profile import MappingProfile, ValueFormatRule
from snowflake_semantic_sync.models.lookml import (
    LookMLDimension,
    LookMLDimensionGroup,
    LookMLMeasure,
)
from snowflake_semantic_sync.models.snowflake import parse_semantic_view_yaml

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sample_semantic_view.yaml"


def test_map_and_render_analytic_model_view_with_dimension_groups():
    raw_yaml = FIXTURE_PATH.read_text(encoding="utf-8")
    sv = parse_semantic_view_yaml(raw_yaml, database="ANALYTICS_DB", schema_name="GOLD")

    profile = MappingProfile(
        qualify_analytic_model_name="database_schema",
        quote_identifiers=False,
        include_facts=False,
        use_dimension_groups=True,
        measure_value_format_rules=[
            ValueFormatRule(pattern="revenue", value_format_name="usd"),
            ValueFormatRule(pattern="count", value_format_name="decimal_0"),
        ],
    )
    mapper = SemanticModelMapper(profile)
    view = mapper.map_semantic_view_to_lookml_view(sv)

    assert view.view_name == "tpch_rev_analysis"
    assert view.sql_analytic_model_name == "ANALYTICS_DB.GOLD.TPCH_REV_ANALYSIS"

    # 2 time_dimensions become dimension_groups; 6 non-temporal dimensions stay dimensions
    dg_by_name = {dg.name: dg for dg in view.dimension_groups}
    assert set(dg_by_name.keys()) == {"order", "order_created_at"}
    assert dg_by_name["order"].datatype == "date"
    assert dg_by_name["order"].convert_tz is False
    assert dg_by_name["order"].timeframes == ["raw", "date", "week", "month", "quarter", "year"]
    assert dg_by_name["order"].sql == "${TABLE}.order_date"
    assert dg_by_name["order"].synonyms == ["date ordered", "purchase date"]

    assert dg_by_name["order_created_at"].datatype == "timestamp"
    assert dg_by_name["order_created_at"].convert_tz is False
    assert dg_by_name["order_created_at"].timeframes == ["raw", "time", "date", "week", "month", "quarter", "year"]
    assert dg_by_name["order_created_at"].sql == "${TABLE}.order_created_at"

    dim_by_name = {d.name: d for d in view.dimensions}
    assert len(dim_by_name) == 6
    assert dim_by_name["order_status"].type == "string"
    assert dim_by_name["order_status"].sql == "${TABLE}.order_status"
    assert dim_by_name["order_status"].synonyms == ["status", "fulfillment status"]
    assert dim_by_name["order_status"].suggestions == ["F", "O", "P"]
    assert dim_by_name["order_status"].group_label == "Orders"
    assert dim_by_name["is_expedited"].type == "yesno"
    assert dim_by_name["account_balance_tier"].type == "number"

    meas_by_name = {m.name: m for m in view.measures}
    assert len(meas_by_name) == 4
    assert meas_by_name["total_revenue"].type == "number"
    assert meas_by_name["total_revenue"].sql == "${TABLE}.total_revenue"
    assert meas_by_name["total_revenue"].value_format_name == "usd"
    assert meas_by_name["order_count"].value_format_name == "decimal_0"
    assert meas_by_name["average_revenue_per_customer"].group_label is None

    generator = LookMLGenerator()
    rendered_view = generator.render_base_view(view)
    assert rendered_view.endswith("\n")
    assert "sql_analytic_model_name: ANALYTICS_DB.GOLD.TPCH_REV_ANALYSIS ;;" in rendered_view
    assert "dimension_group: order {" in rendered_view

    # Verify round-trip parsing with lkml
    parsed = lkml.load(rendered_view)
    assert parsed["views"][0]["name"] == "tpch_rev_analysis"
    assert parsed["views"][0]["sql_analytic_model_name"] == "ANALYTICS_DB.GOLD.TPCH_REV_ANALYSIS"
    assert len(parsed["views"][0]["dimension_groups"]) == 2

    # Verify base explore rendering
    explore = mapper.map_semantic_view_to_explore(sv, view)
    rendered_explore = generator.render_base_explore(explore)
    assert rendered_explore.endswith("\n")
    parsed_explore = lkml.load(rendered_explore)
    assert parsed_explore["explores"][0]["name"] == "tpch_rev_analysis"


def test_flat_temporal_dimensions_when_dimension_groups_disabled():
    raw_yaml = FIXTURE_PATH.read_text(encoding="utf-8")
    sv = parse_semantic_view_yaml(raw_yaml, database="ANALYTICS_DB", schema_name="GOLD")
    mapper = SemanticModelMapper(MappingProfile(use_dimension_groups=False))
    view = mapper.map_semantic_view_to_lookml_view(sv)

    assert view.dimension_groups == []
    dim_by_name = {d.name: d for d in view.dimensions}
    assert len(dim_by_name) == 8
    assert dim_by_name["order_date"].type == "date"
    assert dim_by_name["order_created_at"].type == "date_time"


def test_colliding_names_disambiguate_lookml_name_and_3part_sql():
    raw_yaml = """
name: MULTI_STATION_TRIPS
tables:
  - name: START_STATIONS
    dimensions:
      - name: STATION_NAME
        data_type: VARCHAR
      - name: UPDATED_TIME
        data_type: TIMESTAMP_TZ
  - name: END_STATIONS
    dimensions:
      - name: STATION_NAME
        data_type: VARCHAR
  - name: TRIPS
    dimensions:
      - name: START
        data_type: VARCHAR
      - name: START_TIME
        data_type: TIMESTAMP_NTZ(9)
    metrics:
      - name: TRIP_COUNT
        expr: COUNT(1)
"""
    sv = parse_semantic_view_yaml(raw_yaml, database="ANALYTICS_DB", schema_name="PUBLIC")
    view = SemanticModelMapper(MappingProfile()).map_semantic_view_to_lookml_view(sv)

    dim_by_name = {d.name: d for d in view.dimensions}
    assert dim_by_name["start_stations_station_name"].sql == "${TABLE}.START_STATIONS.STATION_NAME"
    assert dim_by_name["end_stations_station_name"].sql == "${TABLE}.END_STATIONS.STATION_NAME"
    # Non-colliding field uses compact 2-part ${TABLE}.<FIELD>
    assert dim_by_name["start"].sql == "${TABLE}.START"

    dg_by_name = {dg.name: dg for dg in view.dimension_groups}
    # Because 'start' already exists as a dimension, START_TIME must NOT strip '_time'
    assert "start_time" in dg_by_name
    assert dg_by_name["start_time"].convert_tz is False
    # TIMESTAMP_TZ leaves convert_tz unset (None) so Looker can convert timezones
    assert "updated" in dg_by_name
    assert dg_by_name["updated"].convert_tz is None


def test_invalid_analytic_model_field_type_rejected():
    with pytest.raises(ValueError, match="Unsupported LookML field type"):
        LookMLDimension(name="bad_dim", type="tier", sql="${TABLE}.bad_dim")


def test_invalid_dimension_group_duration_rejected():
    with pytest.raises(ValueError, match="Only 'time' is supported"):
        LookMLDimensionGroup(name="elapsed", type="duration", sql="${TABLE}.START_TIME")


def test_standalone_time_dimension_and_measure_rejected():
    with pytest.raises(ValueError, match="dimension_group"):
        LookMLDimension(name="obs_time", type="time", sql="${TABLE}.OBS_TIME")
    with pytest.raises(ValueError, match="Unsupported LookML measure type"):
        LookMLMeasure(name="bad_time_metric", type="time", sql="${TABLE}.OBS_TIME")


def test_snowflake_time_columns_map_to_string_dimensions():
    raw_yaml = """
name: WEATHER_TIMES
tables:
  - name: WEATHER
    dimensions:
      - name: OBS_TIME
        data_type: TIME(9)
    time_dimensions:
      - name: ALT_TIME
        data_type: TIME
      - name: OBS_DATE
        data_type: DATE
    metrics:
      - name: OBS_COUNT
        expr: COUNT(1)
"""
    sv = parse_semantic_view_yaml(raw_yaml, database="DEMO_DB", schema_name="PUBLIC")

    # Even if a custom profile erroneously maps TIME -> time, mapper coerces to string
    legacy_profile = MappingProfile()
    legacy_profile.type_mappings["TIME"] = "time"
    view = SemanticModelMapper(legacy_profile).map_semantic_view_to_lookml_view(sv)

    dim_by_name = {d.name: d for d in view.dimensions}
    assert dim_by_name["obs_time"].type == "string"
    assert dim_by_name["alt_time"].type == "string"
    assert [dg.name for dg in view.dimension_groups] == ["obs"]

    rendered = LookMLGenerator().render_base_view(view)
    assert "type: time" in rendered  # dimension_group only
    parsed = lkml.load(rendered)
    for dim in parsed["views"][0]["dimensions"]:
        assert dim["type"] != "time"


def test_quoted_view_quoted_tables_and_canonical_collision_normalization():
    raw_yaml = """
name: '"SV Lab 11 Quoted View"'
tables:
  - name: '"Customer Records"'
    dimensions:
      - name: '"NAME"'
        data_type: VARCHAR(25)
      - name: '"Label"'
        data_type: VARCHAR(10)
      - name: '"Display Name"'
        data_type: VARCHAR(25)
      - name: CREATED_TIME
        data_type: TIME(9)
    metrics:
      - name: '"Total Balance ($)"'
        expr: SUM(C_ACCTBAL)
  - name: '"Nation Records"'
    dimensions:
      - name: NAME
        data_type: VARCHAR(25)
      - name: LABEL
        data_type: VARCHAR(25)
      - name: '"Display Name"'
        data_type: VARCHAR(25)
"""
    sv = parse_semantic_view_yaml(raw_yaml, database='"My-DB"', schema_name='"My Schema"')
    view = SemanticModelMapper(MappingProfile(quote_identifiers=False)).map_semantic_view_to_lookml_view(sv)

    assert view.view_name == "sv_lab_11_quoted_view"
    assert view.sql_analytic_model_name == '"My-DB"."My Schema"."SV Lab 11 Quoted View"'

    dim_by_name = {d.name: d for d in view.dimensions}
    # '"NAME"' and 'NAME' both canonicalize to 'NAME' -> 3-part qualified with quoted logical tables
    assert dim_by_name["customer_records_name"].sql == '${TABLE}."Customer Records"."NAME"'
    assert dim_by_name["nation_records_name"].sql == '${TABLE}."Nation Records".NAME'
    # Identical quoted spaces collide -> 3-part qualified without double-wrapping quotes
    assert dim_by_name["customer_records_display_name"].sql == '${TABLE}."Customer Records"."Display Name"'
    assert dim_by_name["nation_records_display_name"].sql == '${TABLE}."Nation Records"."Display Name"'
    # Mixed-case '"Label"' vs unquoted 'LABEL' disambiguate in LookML, stay distinct in Snowflake SQL
    assert dim_by_name["customer_records_label"].sql == '${TABLE}."Label"'
    assert dim_by_name["nation_records_label"].sql == "${TABLE}.LABEL"
    # TIME(9) maps to string
    assert dim_by_name["created_time"].type == "string"

    meas_by_name = {m.name: m for m in view.measures}
    assert meas_by_name["total_balance"].sql == '${TABLE}."Total Balance ($)"'

    rendered = LookMLGenerator().render_base_view(view)
    parsed = lkml.load(rendered)
    assert parsed["views"][0]["sql_analytic_model_name"] == '"My-DB"."My Schema"."SV Lab 11 Quoted View"'


def test_temporal_and_prefixed_metric_collisions_never_drop_or_duplicate_fields():
    raw_yaml = """
name: COLLISION_EDGE_CASES
tables:
  - name: ORDERS
    time_dimensions:
      - name: CREATED_TIME
        data_type: TIMESTAMP_NTZ
      - name: CREATED_DATE
        data_type: DATE
    dimensions:
      - name: ORDERS_TOTAL
        data_type: NUMBER
    metrics:
      - name: TOTAL
        expr: SUM(1)
  - name: LINEITEMS
    metrics:
      - name: TOTAL
        expr: SUM(2)
"""
    sv = parse_semantic_view_yaml(raw_yaml, database="DEMO_DB", schema_name="PUBLIC")
    view = SemanticModelMapper(MappingProfile()).map_semantic_view_to_lookml_view(sv)

    # Neither created_time nor created_date may be dropped, and no expanded timeframe may collide
    assert len(view.dimension_groups) == 2
    expanded_names: set[str] = set()
    for dg in view.dimension_groups:
        assert dg.name not in expanded_names
        expanded_names.add(dg.name)
        for tf in dg.timeframes:
            tf_name = f"{dg.name}_{tf}"
            assert tf_name not in expanded_names
            expanded_names.add(tf_name)

    # All dimensions and measures have unique names across the entire view
    for d in view.dimensions:
        assert d.name not in expanded_names
        expanded_names.add(d.name)
    for m in view.measures:
        assert m.name not in expanded_names
        expanded_names.add(m.name)

    meas_names = {m.name for m in view.measures}
    assert meas_names == {"orders_total_metric", "lineitems_total"}
    assert LookMLGenerator().render_base_view(view)
