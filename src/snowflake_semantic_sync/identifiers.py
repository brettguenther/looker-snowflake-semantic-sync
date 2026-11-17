"""Snowflake identifier validation, SQL quoting, and case-normalization utilities."""

import re
from typing import Tuple

MAX_SNOWFLAKE_IDENTIFIER_LENGTH = 255
_UNQUOTED_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,254}$")
_UPPER_UNQUOTED_IDENTIFIER_RE = re.compile(r"^[A-Z_][A-Z0-9_$]{0,254}$")
# Reject SQL injection metacharacters and LookML block terminators
_DANGEROUS_SQL_RE = re.compile(r"(?:;|--|/\*|\*/|['`\\])")


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def unquote_snowflake_identifier(identifier: str) -> Tuple[str, bool]:
    """Unwraps a double-quoted Snowflake identifier, unescaping internal ''\"\"'' pairs.

    Returns:
        (inner_value, was_quoted)
    """
    cleaned = (identifier or "").strip()
    if len(cleaned) >= 2 and cleaned.startswith('"') and cleaned.endswith('"'):
        body = cleaned[1:-1]
        if '"' in body.replace('""', ""):
            raise ValueError(
                f"Invalid Snowflake identifier '{identifier}': unescaped double quote inside quoted identifier."
            )
        return body.replace('""', '"'), True
    return cleaned, False


def validate_snowflake_identifier(
    identifier: str,
    label: str = "identifier",
    *,
    from_metadata: bool = False,
) -> str:
    """Validates and normalizes a Snowflake object identifier for safe SQL execution.

    - Supports standard unquoted identifiers (preserving Snowflake case-insensitive resolution)
      as well as double-quoted and special-character identifiers (spaces, hyphens, mixed case,
      parentheses, etc.).
    - When ``from_metadata=True``, ``identifier`` is treated as an already-unquoted exact object
      name returned by Snowflake (e.g. ``SHOW SEMANTIC VIEWS``), so lowercase or special-character
      names are automatically double-quoted to preserve exact case.
    - Rejects empty strings, overlong (>255 chars) names, control characters, and SQL injection
      metacharacters (``;``, ``--``, ``/*``, ``*/``, single quotes, backticks, backslashes).
    """
    if not identifier or not identifier.strip():
        raise ValueError(f"Invalid Snowflake {label} '{identifier}': identifier cannot be empty.")

    if _has_control_chars(identifier):
        raise ValueError(f"Invalid Snowflake {label} '{identifier}': control characters are not allowed.")

    if from_metadata:
        raw = identifier
        if len(raw) > MAX_SNOWFLAKE_IDENTIFIER_LENGTH:
            raise ValueError(
                f"Invalid Snowflake {label} '{identifier}': exceeds {MAX_SNOWFLAKE_IDENTIFIER_LENGTH} characters."
            )
        if ";;" in raw:
            raise ValueError(f"Invalid Snowflake {label} '{identifier}': LookML terminator ';;' is not allowed.")
        if _UPPER_UNQUOTED_IDENTIFIER_RE.match(raw):
            return raw
        escaped = raw.replace('"', '""')
        return f'"{escaped}"'

    cleaned = identifier.strip()
    if cleaned.startswith('"') or cleaned.endswith('"'):
        if not (len(cleaned) >= 2 and cleaned.startswith('"') and cleaned.endswith('"')):
            raise ValueError(f"Invalid Snowflake {label} '{identifier}': unbalanced double quotes.")
        inner, _ = unquote_snowflake_identifier(cleaned)
        if not inner or len(inner) > MAX_SNOWFLAKE_IDENTIFIER_LENGTH:
            raise ValueError(
                f"Invalid Snowflake {label} '{identifier}': identifier length must be between 1 and "
                f"{MAX_SNOWFLAKE_IDENTIFIER_LENGTH} characters."
            )
        if _DANGEROUS_SQL_RE.search(inner):
            raise ValueError(f"Invalid Snowflake {label} '{identifier}': disallowed SQL characters detected.")
        escaped = inner.replace('"', '""')
        return f'"{escaped}"'

    if len(cleaned) > MAX_SNOWFLAKE_IDENTIFIER_LENGTH:
        raise ValueError(
            f"Invalid Snowflake {label} '{identifier}': exceeds {MAX_SNOWFLAKE_IDENTIFIER_LENGTH} characters."
        )
    if '"' in cleaned or _DANGEROUS_SQL_RE.search(cleaned):
        raise ValueError(
            f"Invalid Snowflake {label} '{identifier}': disallowed characters detected. "
            "Wrap identifiers containing double quotes properly and avoid SQL metacharacters."
        )
    if _UNQUOTED_IDENTIFIER_RE.match(cleaned):
        return cleaned
    return f'"{cleaned}"'


def format_sql_identifier(identifier: str, *, force_quote: bool = False) -> str:
    """Formats a Snowflake identifier for SQL / LookML emission without stripping needed quotes."""
    inner, was_quoted = unquote_snowflake_identifier(identifier)
    if not inner:
        return ""
    needs_quote = force_quote or was_quoted or not _UNQUOTED_IDENTIFIER_RE.match(inner)
    if needs_quote:
        escaped = inner.replace('"', '""')
        return f'"{escaped}"'
    return inner


def canonical_snowflake_identifier(identifier: str) -> str:
    """Returns Snowflake's resolved identifier name for SQL collision and key matching.

    Explicitly double-quoted identifiers (and identifiers requiring quotes) preserve exact case;
    unquoted identifiers fold to uppercase per Snowflake SQL identifier semantics.
    """
    inner, was_quoted = unquote_snowflake_identifier(identifier)
    if was_quoted or not _UNQUOTED_IDENTIFIER_RE.match(inner):
        return inner
    return inner.upper()
