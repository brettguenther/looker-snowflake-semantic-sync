"""Snowflake Client supporting Key-Pair (PKCS#8) and OAuth authentication for Semantic View extraction."""

import base64
import json
from pathlib import Path
import re
from typing import Any, Callable, Dict, List, Optional
import urllib.error
import urllib.parse
import urllib.request

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
import snowflake.connector

from snowflake_semantic_sync.models.snowflake import (
    SnowflakeSemanticView,
    parse_semantic_view_yaml,
)

_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
SUPPORTED_AUTH_MODES = ("key_pair", "oauth", "oauth_client_credentials", "externalbrowser")


def validate_snowflake_identifier(identifier: str, label: str = "identifier") -> str:
    """Validates a Snowflake identifier against a strict allowlist to prevent SQL injection."""
    cleaned = (identifier or "").strip()
    if not cleaned or not _SAFE_IDENTIFIER_RE.match(cleaned):
        raise ValueError(
            f"Invalid Snowflake {label} '{identifier}'. Only alphanumeric characters, '_', and '$' are allowed."
        )
    return cleaned


def load_private_key_der_bytes(
    private_key_path: Optional[str] = None,
    private_key_raw: Optional[str] = None,
    passphrase: Optional[str] = None,
) -> bytes:
    """Loads an RSA PKCS#8 private key from a file path or raw/base64 PEM string and returns DER bytes."""
    pem_bytes: Optional[bytes] = None

    if private_key_path and private_key_path.strip():
        resolved_path = Path(private_key_path.strip()).expanduser().resolve(strict=True)
        if not resolved_path.is_file():
            raise ValueError(f"Snowflake private key path is not a file: {resolved_path}")
        pem_bytes = resolved_path.read_bytes()
    elif private_key_raw and private_key_raw.strip():
        raw_str = private_key_raw.strip().replace("\\n", "\n")
        if "-----BEGIN" in raw_str:
            pem_bytes = raw_str.encode("utf-8")
        else:
            try:
                pem_bytes = base64.b64decode(raw_str)
            except Exception as e:
                raise ValueError("Invalid SNOWFLAKE_PRIVATE_KEY_RAW: neither PEM header nor valid base64.") from e
    else:
        raise ValueError(
            "Key-pair authentication requires either 'snowflake_private_key_path' "
            "(SNOWFLAKE_PRIVATE_KEY_PATH) or 'snowflake_private_key_raw' (SNOWFLAKE_PRIVATE_KEY_RAW)."
        )

    password_bytes = passphrase.encode("utf-8") if passphrase else None
    private_key = serialization.load_pem_private_key(
        pem_bytes,
        password=password_bytes,
        backend=default_backend(),
    )
    return private_key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def fetch_oauth_client_credentials_token(
    token_url: str,
    client_id: str,
    client_secret: str,
    scope: Optional[str] = None,
) -> str:
    """Exchanges OAuth 2.0 Client Credentials for an access token over HTTPS."""
    parsed = urllib.parse.urlparse(token_url.strip())
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise ValueError("SNOWFLAKE_OAUTH_TOKEN_URL must be a valid HTTPS URL.")
    if not client_id or not client_secret:
        raise ValueError(
            "OAuth Client Credentials flow requires SNOWFLAKE_OAUTH_CLIENT_ID and SNOWFLAKE_OAUTH_CLIENT_SECRET."
        )

    form_data: Dict[str, str] = {"grant_type": "client_credentials"}
    if scope and scope.strip():
        form_data["scope"] = scope.strip()

    encoded_body = urllib.parse.urlencode(form_data).encode("utf-8")
    basic_creds = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")

    req = urllib.request.Request(
        token_url.strip(),
        data=encoded_body,
        headers={
            "Authorization": f"Basic {basic_creds}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"OAuth token exchange failed with HTTP {e.code}.") from None

    token = payload.get("access_token")
    if not token or not isinstance(token, str):
        raise RuntimeError("OAuth token endpoint response did not contain 'access_token'.")
    return token


class SnowflakeSemanticClient:
    """Extracts Snowflake Semantic Views via SHOW SEMANTIC VIEWS and SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW."""

    def __init__(
        self,
        account: str,
        user: Optional[str] = None,
        role: Optional[str] = None,
        warehouse: Optional[str] = None,
        database: Optional[str] = None,
        schema: Optional[str] = None,
        auth_mode: str = "key_pair",
        private_key_path: Optional[str] = None,
        private_key_raw: Optional[str] = None,
        private_key_passphrase: Optional[str] = None,
        oauth_token: Optional[str] = None,
        oauth_client_id: Optional[str] = None,
        oauth_client_secret: Optional[str] = None,
        oauth_token_url: Optional[str] = None,
        oauth_scope: Optional[str] = None,
        connection_factory: Optional[Callable[..., Any]] = None,
    ):
        self.account = (account or "").strip()
        self.user = (user or "").strip() or None
        self.role = (role or "").strip() or None
        self.warehouse = (warehouse or "").strip() or None
        self.database = (database or "").strip() or None
        self.schema = (schema or "").strip() or None
        self.auth_mode = (auth_mode or "key_pair").strip().lower()
        self.private_key_path = private_key_path
        self.private_key_raw = private_key_raw
        self.private_key_passphrase = private_key_passphrase
        self.oauth_token = oauth_token
        self.oauth_client_id = oauth_client_id
        self.oauth_client_secret = oauth_client_secret
        self.oauth_token_url = oauth_token_url
        self.oauth_scope = oauth_scope
        self._connection_factory = connection_factory or snowflake.connector.connect

        if self.auth_mode not in SUPPORTED_AUTH_MODES:
            raise ValueError(
                f"Unsupported snowflake_auth_mode '{self.auth_mode}'. "
                f"Supported modes: {', '.join(SUPPORTED_AUTH_MODES)}"
            )

    def build_connect_kwargs(self) -> Dict[str, Any]:
        """Builds validated connection keyword arguments for snowflake.connector.connect."""
        if not self.account:
            raise ValueError("Snowflake account identifier (SNOWFLAKE_ACCOUNT) is required.")

        kwargs: Dict[str, Any] = {
            "account": self.account,
        }
        if self.user:
            kwargs["user"] = self.user
        if self.role:
            kwargs["role"] = self.role
        if self.warehouse:
            kwargs["warehouse"] = self.warehouse
        if self.database:
            kwargs["database"] = self.database
        if self.schema:
            kwargs["schema"] = self.schema

        if self.auth_mode == "key_pair":
            if not self.user:
                raise ValueError("SNOWFLAKE_USER is required for key_pair authentication.")
            kwargs["private_key"] = load_private_key_der_bytes(
                private_key_path=self.private_key_path,
                private_key_raw=self.private_key_raw,
                passphrase=self.private_key_passphrase,
            )
        elif self.auth_mode == "oauth":
            if not self.oauth_token or not self.oauth_token.strip():
                raise ValueError("SNOWFLAKE_OAUTH_TOKEN is required when snowflake_auth_mode='oauth'.")
            kwargs["authenticator"] = "oauth"
            kwargs["token"] = self.oauth_token.strip()
        elif self.auth_mode == "oauth_client_credentials":
            if not self.oauth_token_url:
                raise ValueError(
                    "SNOWFLAKE_OAUTH_TOKEN_URL is required when snowflake_auth_mode='oauth_client_credentials'."
                )
            token = fetch_oauth_client_credentials_token(
                token_url=self.oauth_token_url,
                client_id=self.oauth_client_id or "",
                client_secret=self.oauth_client_secret or "",
                scope=self.oauth_scope,
            )
            kwargs["authenticator"] = "oauth"
            kwargs["token"] = token
        elif self.auth_mode == "externalbrowser":
            if not self.user:
                raise ValueError("SNOWFLAKE_USER is required for externalbrowser authentication.")
            kwargs["authenticator"] = "externalbrowser"
            kwargs["client_store_temporary_credential"] = True

        return kwargs

    def _connect(self) -> Any:
        connect_kwargs = self.build_connect_kwargs()
        if self._connection_factory is snowflake.connector.connect:
            connect_kwargs.setdefault("enable_server_session_keep_alive_auto_detection", False)
        return self._connection_factory(**connect_kwargs)

    def list_semantic_views(self, database: str, schema: str) -> List[str]:
        """Executes SHOW SEMANTIC VIEWS IN SCHEMA <database>.<schema> and returns view names."""
        safe_db = validate_snowflake_identifier(database, "database")
        safe_schema = validate_snowflake_identifier(schema, "schema")

        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SHOW SEMANTIC VIEWS IN SCHEMA {safe_db}.{safe_schema}")
                col_names = [desc[0].lower() for desc in (cur.description or [])]
                name_idx = col_names.index("name") if "name" in col_names else 1
                rows = cur.fetchall() or []
                views: List[str] = []
                for row in rows:
                    if isinstance(row, dict):
                        val = row.get("name") or row.get("NAME")
                    else:
                        val = row[name_idx] if len(row) > name_idx else None
                    if val:
                        views.append(str(val).strip())
                return views
        finally:
            conn.close()

    def read_semantic_view_yaml(self, database: str, schema: str, view_name: str) -> str:
        """Executes SELECT SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW('<db>.<schema>.<view>') using a bound parameter."""
        safe_db = validate_snowflake_identifier(database, "database")
        safe_schema = validate_snowflake_identifier(schema, "schema")
        safe_view = validate_snowflake_identifier(view_name, "semantic view")
        fqn = f"{safe_db}.{safe_schema}.{safe_view}"

        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW(%s)", (fqn,))
                row = cur.fetchone()
                if not row:
                    raise RuntimeError(f"SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW returned no rows for '{fqn}'.")
                val = next(iter(row.values())) if isinstance(row, dict) else row[0]
                if not val or not str(val).strip():
                    raise RuntimeError(f"SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW returned empty YAML for '{fqn}'.")
                return str(val)
        finally:
            conn.close()

    def get_semantic_views(
        self,
        database: str,
        schema: str,
        view_names: Optional[List[str]] = None,
    ) -> List[SnowflakeSemanticView]:
        """Discovers and extracts Snowflake Semantic Views into normalized AST models."""
        safe_db = validate_snowflake_identifier(database, "database")
        safe_schema = validate_snowflake_identifier(schema, "schema")

        target_names = (
            [validate_snowflake_identifier(v, "semantic view") for v in view_names if v and v.strip()]
            if view_names
            else []
        )

        conn = self._connect()
        try:
            with conn.cursor() as cur:
                if not target_names:
                    cur.execute(f"SHOW SEMANTIC VIEWS IN SCHEMA {safe_db}.{safe_schema}")
                    col_names = [desc[0].lower() for desc in (cur.description or [])]
                    name_idx = col_names.index("name") if "name" in col_names else 1
                    for row in cur.fetchall() or []:
                        val = (
                            (row.get("name") or row.get("NAME"))
                            if isinstance(row, dict)
                            else (row[name_idx] if len(row) > name_idx else None)
                        )
                        if val:
                            target_names.append(validate_snowflake_identifier(str(val), "semantic view"))

                semantic_views: List[SnowflakeSemanticView] = []
                for v_name in target_names:
                    fqn = f"{safe_db}.{safe_schema}.{v_name}"
                    cur.execute("SELECT SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW(%s)", (fqn,))
                    row = cur.fetchone()
                    if not row:
                        continue
                    raw_yaml = next(iter(row.values())) if isinstance(row, dict) else row[0]
                    if raw_yaml and str(raw_yaml).strip():
                        parsed = parse_semantic_view_yaml(
                            raw_yaml=str(raw_yaml),
                            database=safe_db,
                            schema_name=safe_schema,
                            fallback_name=v_name,
                        )
                        semantic_views.append(parsed)
                return semantic_views
        finally:
            conn.close()
