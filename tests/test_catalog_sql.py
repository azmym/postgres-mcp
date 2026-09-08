"""Offline guards for the catalog SQL in server.py.

The introspection queries are only executed against a real PostgreSQL by
tests/test_integration.py, which skips when no server is reachable. These
tests check what can be checked without one.
"""
from __future__ import annotations

import re

import pytest

import server

# Reserved and non-reserved-but-unsafe words that cannot be used as a bare
# column alias in PostgreSQL. Not exhaustive; it covers the words these
# queries plausibly reach for.
RESERVED_WORDS = frozenset({
    "all", "and", "any", "as", "asc", "case", "cast", "check", "collate",
    "column", "constraint", "create", "current_date", "current_role",
    "current_time", "current_timestamp", "current_user", "default",
    "deferrable", "desc", "distinct", "do", "else", "end", "except",
    "false", "fetch", "for", "foreign", "from", "grant", "group", "having",
    "in", "initially", "intersect", "into", "is", "join", "lateral",
    "leading", "limit", "localtime", "localtimestamp", "not", "null",
    "offset", "on", "only", "or", "order", "primary", "references",
    "returning", "select", "session_user", "some", "table", "then", "to",
    "trailing", "true", "union", "unique", "user", "using", "variadic",
    "when", "where", "window", "with",
})

SQL_CONSTANTS = (
    "_TABLE_LIST_SQL",
    "_COLUMNS_SQL",
    "_CONSTRAINTS_SQL",
    "_INDEXES_SQL",
    "_CONNECTION_SQL",
)

# AS followed by either a double-quoted alias or a bare identifier.
_ALIAS_RE = re.compile(r'\bAS\s+("?)([A-Za-z_][A-Za-z0-9_]*)\1', re.IGNORECASE)


def _strip_sql_comments(sql: str) -> str:
    """Remove -- comments so prose about aliases is not mistaken for SQL."""
    return "\n".join(line.split("--")[0] for line in sql.splitlines())


@pytest.mark.parametrize("name", SQL_CONSTANTS)
def test_reserved_word_aliases_are_quoted(name: str) -> None:
    """A bare `AS table` is a syntax error; only tests like this catch it offline."""
    sql = _strip_sql_comments(getattr(server, name))

    offenders = [
        alias
        for quote, alias in _ALIAS_RE.findall(sql)
        if not quote and alias.lower() in RESERVED_WORDS
    ]

    assert offenders == [], (
        f"{name} uses reserved word(s) {offenders} as unquoted column "
        f'aliases; write AS "{offenders[0] if offenders else ""}" instead'
    )


@pytest.mark.parametrize("name", SQL_CONSTANTS)
def test_sql_constants_are_single_statements(name: str) -> None:
    """Each constant must be one statement: psql.build_input appends its own
    terminator, and a stray semicolon would split it."""
    sql = _strip_sql_comments(getattr(server, name)).strip()

    assert sql.count(";") == 0, f"{name} contains a semicolon"
