"""Config loading, validation and read-only precedence.

The config file never holds secrets: `password_env` names an environment
variable, and ~/.pgpass covers the rest.
"""
from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_DEFAULT_TIMEOUT = "30s"
_DEFAULT_MAX_ROWS = 1000

# Interpolated into SQL as SET statement_timeout = '<value>', so the format is
# validated rather than trusted.
_TIMEOUT_RE = re.compile(r"^\d+\s*(?:ms|s|min|h)?$", re.IGNORECASE)

_TRUTHY = frozenset({"1", "true", "yes", "on"})

_DISCRETE_FIELDS = ("host", "port", "user", "dbname", "sslmode")


class ConfigError(Exception):
    """Raised for any malformed or unusable configuration."""


@dataclass(frozen=True)
class Database:
    name: str
    read_only: bool
    statement_timeout: str
    max_rows: int
    dsn: str | None = None
    host: str | None = None
    port: int | None = None
    user: str | None = None
    dbname: str | None = None
    sslmode: str | None = None
    password_env: str | None = None


@dataclass(frozen=True)
class Config:
    databases: dict[str, Database]
    force_read_only: bool

    def get(self, name: str) -> Database:
        try:
            return self.databases[name]
        except KeyError:
            valid = ", ".join(sorted(self.databases)) or "(none configured)"
            raise ConfigError(
                f"unknown database {name!r}; configured databases: {valid}"
            ) from None


def config_path(env: Mapping[str, str] | None = None) -> Path:
    """Return the config file location, honouring POSTGRES_MCP_CONFIG."""
    environ = os.environ if env is None else env
    override = environ.get("POSTGRES_MCP_CONFIG")
    if override:
        return Path(override)
    return Path.home() / ".config" / "postgres-mcp" / "config.toml"


def load_config(
    path: Path | str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    force_read_only: bool = False,
) -> Config:
    """Parse and validate the config file into a Config."""
    environ = os.environ if env is None else env
    resolved = Path(path) if path is not None else config_path(environ)

    if not resolved.exists():
        raise ConfigError(
            f"no config file at {resolved}; create it or set POSTGRES_MCP_CONFIG"
        )

    try:
        with resolved.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{resolved} is not valid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read {resolved}: {exc}") from exc

    forced = force_read_only or environ.get(
        "POSTGRES_MCP_READ_ONLY", ""
    ).strip().lower() in _TRUTHY

    defaults = raw.get("defaults", {})
    if "read_only" in defaults:
        _validate_read_only(defaults["read_only"], "defaults")
    entries = raw.get("databases", {})
    if not entries:
        raise ConfigError(f"no databases configured in {resolved}")

    databases = {
        name: _build_database(name, entry, defaults, environ, forced)
        for name, entry in entries.items()
    }
    return Config(databases=databases, force_read_only=forced)


def _build_database(
    name: str,
    entry: Mapping[str, Any],
    defaults: Mapping[str, Any],
    env: Mapping[str, str],
    forced: bool,
) -> Database:
    dsn = entry.get("dsn")
    discrete = {field: entry.get(field) for field in _DISCRETE_FIELDS}
    supplied = [field for field, value in discrete.items() if value is not None]

    if dsn is not None and supplied:
        raise ConfigError(
            f"database {name!r} sets both dsn and {', '.join(supplied)}; "
            "use one or the other"
        )
    if dsn is None and discrete["dbname"] is None:
        raise ConfigError(
            f"database {name!r} needs either dsn or dbname"
        )

    password_env = entry.get("password_env")
    if password_env is not None and not env.get(password_env):
        raise ConfigError(
            f"database {name!r} names password_env {password_env!r}, "
            "but that environment variable is unset"
        )

    timeout = _validate_timeout(
        entry.get("statement_timeout", defaults.get("statement_timeout", _DEFAULT_TIMEOUT)),
        name,
    )
    max_rows = _validate_max_rows(
        entry.get("max_rows", defaults.get("max_rows", _DEFAULT_MAX_ROWS)), name
    )

    declared = entry.get("read_only", defaults.get("read_only", True))
    read_only = True if forced else _validate_read_only(declared, name)

    return Database(
        name=name,
        read_only=read_only,
        statement_timeout=timeout,
        max_rows=max_rows,
        dsn=dsn,
        host=discrete["host"],
        port=discrete["port"],
        user=discrete["user"],
        dbname=discrete["dbname"],
        sslmode=discrete["sslmode"],
        password_env=password_env,
    )


def _validate_timeout(value: Any, name: str) -> str:
    text = str(value).strip()
    if not _TIMEOUT_RE.match(text):
        raise ConfigError(
            f"database {name!r} has invalid statement_timeout {value!r}; "
            "use a form like 30s, 500ms, 2min or 1h"
        )
    return text


def _validate_max_rows(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ConfigError(
            f"database {name!r} has invalid max_rows {value!r}; "
            "use a positive integer"
        )
    return value


def _validate_read_only(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(
            f"database {name!r} has invalid read_only {value!r}; "
            "use true or false"
        )
    return value
