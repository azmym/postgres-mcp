"""Tests for psql argv, environment and stdin construction."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from postgres_mcp import psql
from postgres_mcp.config import Database


def make_db(**overrides: object) -> Database:
    defaults: dict[str, object] = {
        "name": "test",
        "read_only": True,
        "statement_timeout": "30s",
        "max_rows": 1000,
        "dbname": "app",
    }
    defaults.update(overrides)
    return Database(**defaults)  # type: ignore[arg-type]


def test_argv_always_includes_the_mandatory_flags() -> None:
    argv = psql.build_argv("/bin/psql", make_db(), read_only=True)

    assert "--no-psqlrc" in argv
    assert "--csv" in argv
    assert "-q" in argv
    assert "ON_ERROR_STOP=1" in argv


def test_argv_uses_dsn_as_positional_when_present() -> None:
    db = make_db(dsn="postgresql://me@localhost/app", dbname=None)
    argv = psql.build_argv("/bin/psql", db, read_only=True)

    assert argv[-1] == "postgresql://me@localhost/app"


def test_argv_uses_discrete_flags_when_no_dsn() -> None:
    db = make_db(host="db.example.com", port=5433, user="reader", dbname="app")
    argv = psql.build_argv("/bin/psql", db, read_only=True)

    assert "-h" in argv and "db.example.com" in argv
    assert "-p" in argv and "5433" in argv
    assert "-U" in argv and "reader" in argv
    assert "-d" in argv and "app" in argv


def test_argv_adds_single_transaction_only_in_read_write_mode() -> None:
    read_write = psql.build_argv("/bin/psql", make_db(), read_only=False)
    read_only = psql.build_argv("/bin/psql", make_db(), read_only=True)

    assert "--single-transaction" in read_write
    assert "--single-transaction" not in read_only


def test_argv_passes_psql_variables() -> None:
    argv = psql.build_argv(
        "/bin/psql", make_db(), read_only=True, variables={"schema": "public"}
    )

    assert "schema=public" in argv


def test_password_never_appears_in_argv() -> None:
    db = make_db(password_env="PW")
    argv = psql.build_argv("/bin/psql", db, read_only=True)

    assert not any("secret" in part for part in argv)


def test_password_is_passed_through_the_environment() -> None:
    db = make_db(password_env="PW")
    env = psql.build_env(db, {"PW": "secret", "PATH": "/bin"})

    assert env["PGPASSWORD"] == "secret"


def test_env_sets_sslmode_and_connect_timeout() -> None:
    env = psql.build_env(make_db(sslmode="require"), {})

    assert env["PGSSLMODE"] == "require"
    assert "PGCONNECT_TIMEOUT" in env


def test_env_omits_pgpassword_without_password_env() -> None:
    assert "PGPASSWORD" not in psql.build_env(make_db(), {})


def test_input_wraps_read_only_statements_in_a_read_only_transaction() -> None:
    text = psql.build_input("SELECT 1", make_db(), read_only=True)

    assert "BEGIN READ ONLY;" in text
    assert "ROLLBACK;" in text
    assert "SELECT 1" in text


def test_input_sets_statement_timeout_in_read_only_mode() -> None:
    text = psql.build_input("SELECT 1", make_db(), read_only=True)
    assert "SET statement_timeout = '30s';" in text


def test_input_sets_statement_timeout_in_read_write_mode_too() -> None:
    """A runaway statement must not hang the call in either mode."""
    text = psql.build_input("DELETE FROM t", make_db(), read_only=False)

    assert "SET statement_timeout = '30s';" in text
    assert "BEGIN READ ONLY;" not in text


def test_input_terminates_an_unterminated_statement() -> None:
    text = psql.build_input("SELECT 1", make_db(), read_only=True)
    lines = text.splitlines()
    assert lines[lines.index("SELECT 1") + 1] == ";"


def test_input_terminates_on_its_own_line_after_a_line_comment() -> None:
    """A trailing line comment must not swallow the terminator into itself."""
    text = psql.build_input("SELECT 1 -- note", make_db(), read_only=True)
    lines = text.splitlines()
    assert lines[lines.index("SELECT 1 -- note") + 1] == ";"


def test_input_does_not_duplicate_an_existing_terminator() -> None:
    text = psql.build_input("SELECT 1;", make_db(), read_only=True)
    lines = text.splitlines()
    assert "SELECT 1;" in lines
    assert ";" not in lines


def test_input_terminates_after_a_semicolon_inside_a_line_comment() -> None:
    """A ; inside a trailing comment is not a terminator, so one is emitted."""
    text = psql.build_input("SELECT 1 -- note;", make_db(), read_only=True)
    lines = text.splitlines()
    assert lines[lines.index("SELECT 1 -- note;") + 1] == ";"


def test_input_no_extra_terminator_after_block_comment_semicolon() -> None:
    text = psql.build_input("SELECT 1 /* c */;", make_db(), read_only=True)
    lines = text.splitlines()
    assert ";" not in lines


def test_input_no_extra_terminator_after_trailing_comment() -> None:
    text = psql.build_input("SELECT 1; -- note", make_db(), read_only=True)
    lines = text.splitlines()
    assert ";" not in lines


def test_run_sql_rejects_write_in_read_only_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate runs before any process is spawned."""
    spawned = MagicMock()
    monkeypatch.setattr(psql.subprocess, "run", spawned)
    monkeypatch.setattr(psql, "find_psql", lambda *a, **k: "/bin/psql")

    with pytest.raises(psql.GuardRejected):
        psql.run_sql(make_db(), "DELETE FROM t", read_only=True)

    spawned.assert_not_called()


def test_run_sql_returns_stdout_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    completed = MagicMock(returncode=0, stdout="n\n1\n", stderr="")
    monkeypatch.setattr(psql.subprocess, "run", lambda *a, **k: completed)
    monkeypatch.setattr(psql, "find_psql", lambda *a, **k: "/bin/psql")

    result = psql.run_sql(make_db(), "SELECT 1", read_only=True)

    assert result.ok is True
    assert result.stdout == "n\n1\n"


def test_run_sql_reports_failure_with_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    completed = MagicMock(returncode=1, stdout="", stderr='ERROR: relation "t" does not exist')
    monkeypatch.setattr(psql.subprocess, "run", lambda *a, **k: completed)
    monkeypatch.setattr(psql, "find_psql", lambda *a, **k: "/bin/psql")

    result = psql.run_sql(make_db(), "SELECT * FROM t", read_only=True)

    assert result.ok is False
    assert "does not exist" in result.stderr


def test_run_sql_sends_sql_on_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        captured["argv"] = argv
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(psql.subprocess, "run", fake_run)
    monkeypatch.setattr(psql, "find_psql", lambda *a, **k: "/bin/psql")

    psql.run_sql(make_db(), "SELECT 1", read_only=True)

    assert "SELECT 1" in captured["input"]
    assert "-c" not in captured["argv"]


def test_run_sql_reports_a_timeout_as_a_failed_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TimeoutExpired is not an OSError, so it must not escape to the caller."""

    def raise_timeout(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise psql.subprocess.TimeoutExpired(cmd="psql", timeout=60.0)

    monkeypatch.setattr(psql.subprocess, "run", raise_timeout)
    monkeypatch.setattr(psql, "find_psql", lambda *a, **k: "/bin/psql")

    result = psql.run_sql(make_db(), "SELECT pg_sleep(99)", read_only=True)

    assert result.ok is False
    assert "did not finish" in result.stderr
