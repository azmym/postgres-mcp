"""Tests for config parsing, validation and read-only precedence."""
from __future__ import annotations

from pathlib import Path

import pytest

from postgres_mcp import config as cfg


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body)
    return path


def test_config_path_prefers_env_override() -> None:
    env = {"POSTGRES_MCP_CONFIG": "/tmp/custom.toml"}
    assert cfg.config_path(env) == Path("/tmp/custom.toml")


def test_config_path_defaults_under_dot_config() -> None:
    path = cfg.config_path({})
    assert path.parts[-3:] == (".config", "postgres-mcp", "config.toml")


def test_missing_file_raises_with_path_in_message(tmp_path: Path) -> None:
    missing = tmp_path / "absent.toml"
    with pytest.raises(cfg.ConfigError, match="absent.toml"):
        cfg.load_config(missing, env={})


def test_loads_dsn_entry(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        """
        [databases.local]
        dsn = "postgresql://me@localhost:5432/appdb"
        read_only = false
        """,
    )
    config = cfg.load_config(path, env={})

    assert config.get("local").dsn == "postgresql://me@localhost:5432/appdb"
    assert config.get("local").read_only is False


def test_loads_discrete_field_entry(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        """
        [databases.prod]
        host = "db.example.com"
        port = 5432
        user = "reader"
        dbname = "app"
        sslmode = "require"
        """,
    )
    database = cfg.load_config(path, env={}).get("prod")

    assert database.host == "db.example.com"
    assert database.port == 5432
    assert database.dbname == "app"
    assert database.sslmode == "require"


def test_read_only_defaults_to_true_when_unspecified(tmp_path: Path) -> None:
    """Safe by default: silence means read-only."""
    path = write_config(tmp_path, '[databases.x]\ndbname = "app"\n')
    assert cfg.load_config(path, env={}).get("x").read_only is True


def test_defaults_section_supplies_read_only(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        """
        [defaults]
        read_only = false

        [databases.x]
        dbname = "app"
        """,
    )
    assert cfg.load_config(path, env={}).get("x").read_only is False


def test_per_database_read_only_overrides_defaults(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        """
        [defaults]
        read_only = false

        [databases.x]
        dbname = "app"
        read_only = true
        """,
    )
    assert cfg.load_config(path, env={}).get("x").read_only is True


def test_cli_flag_tightens_read_write_entry(tmp_path: Path) -> None:
    """The global flag can only tighten, never loosen."""
    path = write_config(
        tmp_path, '[databases.x]\ndbname = "app"\nread_only = false\n'
    )
    config = cfg.load_config(path, env={}, force_read_only=True)

    assert config.get("x").read_only is True
    assert config.force_read_only is True


def test_env_flag_tightens_read_write_entry(tmp_path: Path) -> None:
    path = write_config(
        tmp_path, '[databases.x]\ndbname = "app"\nread_only = false\n'
    )
    env = {"POSTGRES_MCP_READ_ONLY": "1"}

    assert cfg.load_config(path, env=env).get("x").read_only is True


def test_dsn_with_discrete_fields_is_an_error(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        """
        [databases.x]
        dsn = "postgresql://localhost/app"
        host = "elsewhere"
        """,
    )
    with pytest.raises(cfg.ConfigError, match="both"):
        cfg.load_config(path, env={})


def test_entry_without_dsn_or_dbname_is_an_error(tmp_path: Path) -> None:
    path = write_config(tmp_path, '[databases.x]\nuser = "me"\n')
    with pytest.raises(cfg.ConfigError, match="dsn"):
        cfg.load_config(path, env={})


def test_no_databases_section_is_an_error(tmp_path: Path) -> None:
    path = write_config(tmp_path, "[defaults]\nread_only = true\n")
    with pytest.raises(cfg.ConfigError, match="no databases"):
        cfg.load_config(path, env={})


def test_password_env_naming_unset_variable_is_an_error(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        '[databases.x]\ndbname = "app"\npassword_env = "MISSING_PW"\n',
    )
    with pytest.raises(cfg.ConfigError, match="MISSING_PW"):
        cfg.load_config(path, env={})


def test_password_env_present_is_accepted(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        '[databases.x]\ndbname = "app"\npassword_env = "PW"\n',
    )
    config = cfg.load_config(path, env={"PW": "secret"})

    assert config.get("x").password_env == "PW"


def test_invalid_statement_timeout_is_an_error(tmp_path: Path) -> None:
    """The value is interpolated into SQL, so its format is validated."""
    path = write_config(
        tmp_path,
        '[databases.x]\ndbname = "app"\nstatement_timeout = "30s; DROP TABLE t"\n',
    )
    with pytest.raises(cfg.ConfigError, match="statement_timeout"):
        cfg.load_config(path, env={})


def test_valid_statement_timeout_units_accepted(tmp_path: Path) -> None:
    for value in ("30s", "500ms", "2min", "1h", "45"):
        assert cfg._validate_timeout(value, "x") == value


def test_non_positive_max_rows_is_an_error(tmp_path: Path) -> None:
    path = write_config(tmp_path, '[databases.x]\ndbname = "app"\nmax_rows = 0\n')
    with pytest.raises(cfg.ConfigError, match="max_rows"):
        cfg.load_config(path, env={})


def test_bool_max_rows_is_an_error(tmp_path: Path) -> None:
    """bool is a subclass of int, so `max_rows = true` must not mean 1."""
    path = write_config(tmp_path, '[databases.x]\ndbname = "app"\nmax_rows = true\n')
    with pytest.raises(cfg.ConfigError, match="max_rows"):
        cfg.load_config(path, env={})


def test_get_unknown_database_lists_valid_names(tmp_path: Path) -> None:
    path = write_config(
        tmp_path, '[databases.alpha]\ndbname = "a"\n\n[databases.beta]\ndbname = "b"\n'
    )
    config = cfg.load_config(path, env={})

    with pytest.raises(cfg.ConfigError, match="alpha, beta"):
        config.get("gamma")
