"""Integration tests against a real Postgres.

Skipped automatically when psql is missing or nothing answers on localhost.
"""
from __future__ import annotations

import socket

import pytest

from postgres_mcp import psql
from postgres_mcp.config import Database


def _postgres_reachable() -> bool:
    try:
        psql.find_psql()
    except psql.PsqlNotFound:
        return False
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("localhost", 5432)) == 0


pytestmark = pytest.mark.skipif(
    not _postgres_reachable(), reason="no local Postgres on localhost:5432"
)


def make_db(read_only: bool) -> Database:
    return Database(
        name="local",
        read_only=read_only,
        statement_timeout="10s",
        max_rows=100,
        dbname="postgres",
    )


def test_select_returns_rows_from_a_live_server() -> None:
    result = psql.run_sql(make_db(True), "SELECT 1 AS n", read_only=True)

    assert result.ok, result.stderr
    assert "1" in result.stdout


def test_read_only_transaction_blocks_a_write_at_the_server() -> None:
    """Layer 3 in isolation: bypass the gate, let Postgres refuse the write."""
    db = make_db(True)
    text = psql.build_input("CREATE TABLE mcp_probe (id int)", db, read_only=True)
    argv = psql.build_argv(psql.find_psql(), db, read_only=True)

    completed = psql.subprocess.run(
        argv,
        input=text,
        env=psql.build_env(db, dict(psql.os.environ)),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "read-only transaction" in completed.stderr.lower()


def test_statement_timeout_is_applied() -> None:
    db = Database(
        name="local",
        read_only=True,
        statement_timeout="100ms",
        max_rows=10,
        dbname="postgres",
    )
    result = psql.run_sql(db, "SELECT pg_sleep(3)", read_only=True)

    assert result.ok is False
    assert "statement timeout" in result.stderr.lower()


@pytest.mark.parametrize(
    "name",
    ["_TABLE_LIST_SQL", "_COLUMNS_SQL", "_CONSTRAINTS_SQL", "_INDEXES_SQL"],
)
def test_introspection_sql_is_valid_against_a_live_server(name: str) -> None:
    """Execute the catalog queries for real.

    The tool-level tests mock psql away, so a SQL syntax error in these
    queries would otherwise reach a user before a test. Reserved words used as
    column aliases (table, column, default, type) are the likely offender and
    fail only when a server parses them.
    """
    import server

    sql = getattr(server, name)
    result = psql.run_sql(
        make_db(True),
        sql,
        read_only=True,
        variables={"schema": "pg_catalog", "table": "pg_class"},
    )

    assert result.ok, f"{name} failed: {result.stderr}"


def test_connection_sql_is_valid_against_a_live_server() -> None:
    import server

    result = psql.run_sql(make_db(True), server._CONNECTION_SQL, read_only=True)

    assert result.ok, result.stderr
    assert "PostgreSQL" in result.stdout
