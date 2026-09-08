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
