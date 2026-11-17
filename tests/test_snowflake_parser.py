"""Unit tests for Snowflake Semantic View YAML parsing."""

from pathlib import Path
from snowflake_semantic_sync.models.snowflake import parse_semantic_view_yaml

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sample_semantic_view.yaml"


def test_parse_semantic_view_yaml():
    raw_yaml = FIXTURE_PATH.read_text(encoding="utf-8")
    sv = parse_semantic_view_yaml(
        raw_yaml=raw_yaml,
        database="ANALYTICS_DB",
        schema_name="GOLD",
    )

    assert sv.name == "TPCH_REV_ANALYSIS"
    assert sv.database == "ANALYTICS_DB"
    assert sv.schema_name == "GOLD"
    assert sv.fully_qualified_name == "ANALYTICS_DB.GOLD.TPCH_REV_ANALYSIS"
    assert len(sv.tables) == 2

    orders_tbl = sv.tables[0]
    assert orders_tbl.name == "orders"
    assert len(orders_tbl.dimensions) == 3
    assert len(orders_tbl.time_dimensions) == 2
    # PRIVATE fact ('internal_discount_factor') must be filtered out automatically
    assert len(orders_tbl.facts) == 1
    assert orders_tbl.facts[0].name == "order_total_price_raw"
    assert len(orders_tbl.metrics) == 2

    customers_tbl = sv.tables[1]
    assert customers_tbl.name == "customers"
    assert len(customers_tbl.dimensions) == 3
    assert len(customers_tbl.metrics) == 1

    assert len(sv.relationships) == 1
    assert sv.relationships[0].left_table == "orders"
    assert sv.relationships[0].right_table == "customers"

    assert len(sv.model_metrics) == 1
    assert sv.model_metrics[0].name == "average_revenue_per_customer"


def test_parse_filters_snowflake_ddl_private_access_modifier():
    raw_yaml = """
name: BIKESHARE_TEST
tables:
  - name: TRIPS
    base_table:
      database: ANALYTICS_DB
      schema: PUBLIC
      table: TRIPS
    dimensions:
      - name: USER_TYPE
        expr: USERTYPE
        data_type: VARCHAR(16777216)
        access_modifier: public_access
      - name: INTERNAL_HASH
        expr: HASH_COL
        data_type: VARCHAR(64)
        access_modifier: private_access
    metrics:
      - name: TOTAL_TRIPS
        expr: COUNT(1)
        access_modifier: public_access
      - name: INTERNAL_RAW_METRIC
        expr: SUM(1)
        access_modifier: private_access
"""
    sv = parse_semantic_view_yaml(raw_yaml, database="ANALYTICS_DB", schema_name="PUBLIC")
    assert [d.name for d in sv.tables[0].dimensions] == ["USER_TYPE"]
    assert [m.name for m in sv.tables[0].metrics] == ["TOTAL_TRIPS"]

