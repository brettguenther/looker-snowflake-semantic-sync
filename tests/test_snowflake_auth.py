"""Unit tests for SnowflakeSemanticClient authentication modes and SQL injection protection."""

import base64
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import pytest

from snowflake_semantic_sync.clients.snowflake import (
    SnowflakeSemanticClient,
    load_private_key_der_bytes,
    validate_snowflake_identifier,
)
from snowflake_semantic_sync.identifiers import (
    canonical_snowflake_identifier,
    format_sql_identifier,
)


def _generate_test_pem(passphrase: bytes | None = None) -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    enc = (
        serialization.BestAvailableEncryption(passphrase)
        if passphrase
        else serialization.NoEncryption()
    )
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=enc,
    )


def test_key_pair_auth_from_file_and_raw(tmp_path: Path):
    pem_bytes = _generate_test_pem()
    key_file = tmp_path / "rsa_key.p8"
    key_file.write_bytes(pem_bytes)

    # 1. File path
    der_from_file = load_private_key_der_bytes(private_key_path=str(key_file))
    assert isinstance(der_from_file, bytes) and len(der_from_file) > 100

    # 2. Raw PEM string
    der_from_raw = load_private_key_der_bytes(private_key_raw=pem_bytes.decode("utf-8"))
    assert der_from_raw == der_from_file

    # 3. Base64-encoded PEM string
    b64_str = base64.b64encode(pem_bytes).decode("ascii")
    der_from_b64 = load_private_key_der_bytes(private_key_raw=b64_str)
    assert der_from_b64 == der_from_file

    # 4. Encrypted PEM with passphrase
    enc_pem = _generate_test_pem(passphrase=b"test-passphrase-123")
    der_enc = load_private_key_der_bytes(
        private_key_raw=enc_pem.decode("utf-8"),
        passphrase="test-passphrase-123",
    )
    assert isinstance(der_enc, bytes) and len(der_enc) > 100


def test_client_build_connect_kwargs_oauth_and_key_pair():
    pem_str = _generate_test_pem().decode("utf-8")
    kp_client = SnowflakeSemanticClient(
        account="acme-xy12345",
        user="SYNC_BOT",
        role="ANALYST_ROLE",
        warehouse="COMPUTE_WH",
        database="ANALYTICS_DB",
        schema="GOLD",
        auth_mode="key_pair",
        private_key_raw=pem_str,
    )
    kp_kwargs = kp_client.build_connect_kwargs()
    assert kp_kwargs["account"] == "acme-xy12345"
    assert kp_kwargs["user"] == "SYNC_BOT"
    assert isinstance(kp_kwargs["private_key"], bytes)

    oauth_client = SnowflakeSemanticClient(
        account="acme-xy12345",
        user="analyst@example.com",
        database="ANALYTICS_DB",
        schema="GOLD",
        auth_mode="oauth",
        oauth_token="example-oauth-access-token",
    )
    oauth_kwargs = oauth_client.build_connect_kwargs()
    assert oauth_kwargs["authenticator"] == "oauth"
    assert oauth_kwargs["token"] == "example-oauth-access-token"

    eb_client = SnowflakeSemanticClient(
        account="acme-xy12345",
        user="analyst@example.com",
        database="ANALYTICS_DB",
        schema="GOLD",
        auth_mode="externalbrowser",
    )
    eb_kwargs = eb_client.build_connect_kwargs()
    assert eb_kwargs["authenticator"] == "externalbrowser"
    assert eb_kwargs["client_store_temporary_credential"] is True


def test_validate_snowflake_identifier_rejects_injection():
    assert validate_snowflake_identifier("TPCH_REV_ANALYSIS") == "TPCH_REV_ANALYSIS"
    assert validate_snowflake_identifier("demo_db") == "demo_db"

    for bad in (
        "",
        "   ",
        "PUBLIC; DROP TABLE users --",
        '"PUBLIC; DROP TABLE users --"',
        "PUBLIC' OR '1'='1",
        'BAD"NAME',
        '"UNBALANCED',
        "FOO\nBAR",
        "FOO\x00BAR",
        "A" * 256,
    ):
        with pytest.raises(ValueError, match="Invalid Snowflake"):
            validate_snowflake_identifier(bad)


def test_validate_and_format_quoted_and_special_identifiers():
    # User-supplied identifiers with spaces/hyphens/parens auto-quote cleanly
    assert validate_snowflake_identifier("SV Lab 11 Quoted View") == '"SV Lab 11 Quoted View"'
    assert validate_snowflake_identifier("my-analytics-db") == '"my-analytics-db"'
    assert validate_snowflake_identifier("Sales (FY26)") == '"Sales (FY26)"'

    # Explicitly double-quoted identifiers preserve exact case and escaped quotes
    assert validate_snowflake_identifier('"SV Lab 11 Quoted View"') == '"SV Lab 11 Quoted View"'
    assert validate_snowflake_identifier('"my_lower_view"') == '"my_lower_view"'
    assert validate_snowflake_identifier('"Sales ""FY26"""') == '"Sales ""FY26"""'

    # Metadata-sourced identifiers from SHOW SEMANTIC VIEWS quote lowercase or special names
    assert validate_snowflake_identifier("SV_LAB_01", from_metadata=True) == "SV_LAB_01"
    assert validate_snowflake_identifier("my_lower_view", from_metadata=True) == '"my_lower_view"'
    assert validate_snowflake_identifier("SV Lab 11 Quoted View", from_metadata=True) == '"SV Lab 11 Quoted View"'
    assert validate_snowflake_identifier('Sales"2026', from_metadata=True) == '"Sales""2026"'

    # Canonical identifier resolution matches Snowflake SQL case-folding rules
    assert canonical_snowflake_identifier('"NAME"') == "NAME"
    assert canonical_snowflake_identifier("NAME") == "NAME"
    assert canonical_snowflake_identifier("name") == "NAME"
    assert canonical_snowflake_identifier('"Label"') == "Label"
    assert canonical_snowflake_identifier("LABEL") == "LABEL"
    assert canonical_snowflake_identifier('"Display Name"') == "Display Name"
    assert canonical_snowflake_identifier("Display Name") == "Display Name"

    # SQL formatting preserves explicit quotes and auto-quotes special chars without double-wrapping
    assert format_sql_identifier("DEMO_DB") == "DEMO_DB"
    assert format_sql_identifier("DEMO_DB", force_quote=True) == '"DEMO_DB"'
    assert format_sql_identifier('"SV Lab 11 Quoted View"') == '"SV Lab 11 Quoted View"'
    assert format_sql_identifier("SV Lab 11 Quoted View") == '"SV Lab 11 Quoted View"'
    assert format_sql_identifier('"my_lower_view"') == '"my_lower_view"'


def test_snowflake_client_discovers_and_reads_quoted_views():
    executed = []

    class FakeCursor:
        def __init__(self):
            self.description = [("created_on",), ("name",)]
            self._rows = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=None):
            executed.append((sql, params))
            if sql.startswith("SHOW SEMANTIC VIEWS"):
                self._rows = [
                    ("2026-11-16", "SV_PLAIN"),
                    ("2026-11-16", "SV Lab 11 Quoted View"),
                    ("2026-11-16", "my_lower_view"),
                ]
            else:
                fqn = params[0]
                vname = fqn.split(".")[-1]
                self._rows = [(f"name: '{vname}'\ntables: []\n",)]

        def fetchall(self):
            return list(self._rows)

        def fetchone(self):
            return self._rows[0] if self._rows else None

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def close(self):
            pass

    client = SnowflakeSemanticClient(
        account="acme-xy12345",
        user="SYNC_BOT",
        database="My-DB",
        schema="My Schema",
        auth_mode="oauth",
        oauth_token="tok",
        connection_factory=lambda **kw: FakeConn(),
    )

    connect_kw = client.build_connect_kwargs()
    assert connect_kw["database"] == '"My-DB"'
    assert connect_kw["schema"] == '"My Schema"'

    views = client.get_semantic_views(database="My-DB", schema="My Schema")
    assert len(views) == 3
    assert executed[0] == ("SHOW SEMANTIC VIEWS IN SCHEMA IDENTIFIER(%s)", ('"My-DB"."My Schema"',))
    assert executed[1] == ("SELECT SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW(%s)", ('"My-DB"."My Schema".SV_PLAIN',))
    assert executed[2] == (
        "SELECT SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW(%s)",
        ('"My-DB"."My Schema"."SV Lab 11 Quoted View"',),
    )
    assert executed[3] == (
        "SELECT SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW(%s)",
        ('"My-DB"."My Schema"."my_lower_view"',),
    )
