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
    with pytest.raises(ValueError, match="Invalid Snowflake"):
        validate_snowflake_identifier("PUBLIC; DROP TABLE users --")
