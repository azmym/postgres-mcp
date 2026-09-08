"""Tool-level tests with psql mocked out."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from postgres_mcp import psql as psql_mod


def test_list_databases_reports_each_entry(fresh_server) -> None:  # type: ignore[no-untyped-def]
    listing = fresh_server.list_databases.fn()
    names = {entry["name"] for entry in listing["databases"]}

    assert names == {"ro", "rw", "withpw"}


def test_list_databases_reports_effective_read_only(fresh_server) -> None:  # type: ignore[no-untyped-def]
    by_name = {e["name"]: e for e in fresh_server.list_databases.fn()["databases"]}

    assert by_name["ro"]["read_only"] is True
    assert by_name["rw"]["read_only"] is False


def test_list_databases_never_leaks_secrets(fresh_server) -> None:  # type: ignore[no-untyped-def]
    """The withpw entry has a real password in the environment; none of it,
    nor the variable name holding it, may appear in the listing."""
    rendered = repr(fresh_server.list_databases.fn())

    assert "super-secret-value" not in rendered
    assert "PGPASSWORD" not in rendered
    assert "password_env" not in rendered


def test_execute_sql_returns_rendered_rows(
    fresh_server, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        fresh_server.psql,
        "run_sql",
        lambda *a, **k: psql_mod.PsqlResult(True, "n\n1\n2\n", "", 0),
    )
    output = fresh_server.execute_sql.fn("ro", "SELECT 1")

    assert "n" in output
    assert "1" in output


def test_execute_sql_passes_read_only_from_config(
    fresh_server, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    """The mode comes from config, never from the caller."""
    captured: dict[str, object] = {}

    def fake_run(db, sql, **kwargs):  # type: ignore[no-untyped-def]
        captured["read_only"] = kwargs["read_only"]
        return psql_mod.PsqlResult(True, "n\n", "", 0)

    monkeypatch.setattr(fresh_server.psql, "run_sql", fake_run)

    fresh_server.execute_sql.fn("rw", "SELECT 1")
    assert captured["read_only"] is False

    fresh_server.execute_sql.fn("ro", "SELECT 1")
    assert captured["read_only"] is True


def test_execute_sql_has_no_read_only_parameter(fresh_server) -> None:  # type: ignore[no-untyped-def]
    """The model must not be able to escalate its own privileges."""
    import inspect

    signature = inspect.signature(fresh_server.execute_sql.fn)
    assert "read_only" not in signature.parameters


def test_execute_sql_unknown_database_lists_valid_names(fresh_server) -> None:  # type: ignore[no-untyped-def]
    output = fresh_server.execute_sql.fn("nope", "SELECT 1")

    assert "ro" in output and "rw" in output


def test_execute_sql_surfaces_guard_rejection(
    fresh_server, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    from postgres_mcp import guard

    rejection = psql_mod.GuardRejected(
        guard.GuardResult(False, "contains DELETE", "DELETE FROM t")
    )
    monkeypatch.setattr(
        fresh_server.psql, "run_sql", MagicMock(side_effect=rejection)
    )
    output = fresh_server.execute_sql.fn("ro", "DELETE FROM t")

    assert "contains DELETE" in output


def test_execute_sql_surfaces_psql_errors(
    fresh_server, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        fresh_server.psql,
        "run_sql",
        lambda *a, **k: psql_mod.PsqlResult(False, "", 'ERROR: no relation "t"', 1),
    )
    output = fresh_server.execute_sql.fn("ro", "SELECT * FROM t")

    assert "no relation" in output


def test_execute_sql_surfaces_install_guidance(
    fresh_server, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    missing = psql_mod.PsqlNotFound("psql was not found. brew install libpq")
    monkeypatch.setattr(
        fresh_server.psql, "run_sql", MagicMock(side_effect=missing)
    )
    output = fresh_server.execute_sql.fn("ro", "SELECT 1")

    assert "brew install libpq" in output


def test_execute_sql_honours_max_rows_override(
    fresh_server, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    csv_text = "n\n" + "".join(f"{i}\n" for i in range(30))
    monkeypatch.setattr(
        fresh_server.psql,
        "run_sql",
        lambda *a, **k: psql_mod.PsqlResult(True, csv_text, "", 0),
    )
    output = fresh_server.execute_sql.fn("ro", "SELECT 1", max_rows=5)

    assert "showing 5 of 30 rows" in output


def test_force_read_only_tightens_a_writable_database(
    fresh_server, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    fresh_server._force_read_only = True
    fresh_server._config = None
    captured: dict[str, object] = {}

    def fake_run(db, sql, **kwargs):  # type: ignore[no-untyped-def]
        captured["read_only"] = kwargs["read_only"]
        return psql_mod.PsqlResult(True, "n\n", "", 0)

    monkeypatch.setattr(fresh_server.psql, "run_sql", fake_run)
    fresh_server.execute_sql.fn("rw", "SELECT 1")

    assert captured["read_only"] is True


def test_describe_schema_lists_tables(
    fresh_server, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        fresh_server.psql,
        "run_sql",
        lambda *a, **k: psql_mod.PsqlResult(True, "table,columns\nusers,4\n", "", 0),
    )
    output = fresh_server.describe_schema.fn("ro")

    assert "users" in output


def test_describe_schema_passes_schema_as_a_psql_variable(
    fresh_server, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    """Identifiers travel as psql variables, never string-formatted into SQL."""
    captured: dict[str, object] = {}

    def fake_run(db, sql, **kwargs):  # type: ignore[no-untyped-def]
        captured["variables"] = kwargs.get("variables")
        captured["sql"] = sql
        return psql_mod.PsqlResult(True, "table,columns\n", "", 0)

    monkeypatch.setattr(fresh_server.psql, "run_sql", fake_run)
    fresh_server.describe_schema.fn("ro", schema="analytics")

    assert captured["variables"] == {"schema": "analytics"}
    assert "analytics" not in captured["sql"]


def test_describe_schema_always_runs_read_only(
    fresh_server, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    """Even against a writable database, introspection only reads."""
    captured: dict[str, object] = {}

    def fake_run(db, sql, **kwargs):  # type: ignore[no-untyped-def]
        captured["read_only"] = kwargs["read_only"]
        return psql_mod.PsqlResult(True, "table,columns\n", "", 0)

    monkeypatch.setattr(fresh_server.psql, "run_sql", fake_run)
    fresh_server.describe_schema.fn("rw")

    assert captured["read_only"] is True


def test_describe_schema_with_table_returns_labelled_sections(
    fresh_server, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        fresh_server.psql,
        "run_sql",
        lambda *a, **k: psql_mod.PsqlResult(True, "a,b\n1,2\n", "", 0),
    )
    output = fresh_server.describe_schema.fn("ro", table="users")

    assert "Columns" in output
    assert "Constraints" in output
    assert "Indexes" in output


def test_describe_schema_rejects_invalid_identifier(fresh_server) -> None:  # type: ignore[no-untyped-def]
    output = fresh_server.describe_schema.fn("ro", table="users; DROP TABLE x")

    assert "invalid" in output.lower()


def test_test_connection_reports_psql_and_server(
    fresh_server, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(fresh_server.psql, "find_psql", lambda *a, **k: "/bin/psql")
    monkeypatch.setattr(
        fresh_server.psql,
        "run_sql",
        lambda *a, **k: psql_mod.PsqlResult(
            True, "version,current_user\nPostgreSQL 18.6,reader\n", "", 0
        ),
    )
    output = fresh_server.test_connection.fn("ro")

    assert "/bin/psql" in output
    assert "PostgreSQL 18.6" in output


def test_test_connection_checks_every_database_when_unspecified(
    fresh_server, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(fresh_server.psql, "find_psql", lambda *a, **k: "/bin/psql")
    monkeypatch.setattr(
        fresh_server.psql,
        "run_sql",
        lambda *a, **k: psql_mod.PsqlResult(True, "version\nPostgreSQL 18.6\n", "", 0),
    )
    output = fresh_server.test_connection.fn()

    assert "ro" in output and "rw" in output


def test_test_connection_reports_install_guidance_when_psql_missing(
    fresh_server, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    def raise_missing(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise psql_mod.PsqlNotFound("psql was not found. brew install libpq")

    monkeypatch.setattr(fresh_server.psql, "find_psql", raise_missing)
    output = fresh_server.test_connection.fn("ro")

    assert "brew install libpq" in output


def test_test_connection_reports_failure_per_database(
    fresh_server, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(fresh_server.psql, "find_psql", lambda *a, **k: "/bin/psql")
    monkeypatch.setattr(
        fresh_server.psql,
        "run_sql",
        lambda *a, **k: psql_mod.PsqlResult(
            False, "", "could not connect to server", 2
        ),
    )
    output = fresh_server.test_connection.fn("ro")

    assert "could not connect" in output


def test_test_connection_reports_read_only_mode(
    fresh_server, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(fresh_server.psql, "find_psql", lambda *a, **k: "/bin/psql")
    monkeypatch.setattr(
        fresh_server.psql,
        "run_sql",
        lambda *a, **k: psql_mod.PsqlResult(True, "version\nPostgreSQL 18.6\n", "", 0),
    )
    output = fresh_server.test_connection.fn()

    assert "ro (read-only)" in output


def test_test_connection_reports_read_write_mode(
    fresh_server, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(fresh_server.psql, "find_psql", lambda *a, **k: "/bin/psql")
    monkeypatch.setattr(
        fresh_server.psql,
        "run_sql",
        lambda *a, **k: psql_mod.PsqlResult(True, "version\nPostgreSQL 18.6\n", "", 0),
    )
    output = fresh_server.test_connection.fn()

    assert "rw (read-write)" in output
