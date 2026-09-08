"""Shared pytest fixtures for postgres-mcp tests."""
from __future__ import annotations

import os

# Make @mcp.tool() return FunctionTool objects so tests can access .fn
os.environ.setdefault("FASTMCP_DECORATOR_MODE", "object")

import pytest

from pathlib import Path


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Write a three-database config and point the server at it.

    The `withpw` entry carries a password_env so the secret-leak test has a
    real secret to look for; without it that assertion is vacuous.
    """
    monkeypatch.setenv("PW", "super-secret-value")
    path = tmp_path / "config.toml"
    path.write_text(
        """
        [databases.ro]
        dbname = "readonly_db"
        read_only = true

        [databases.rw]
        dbname = "writable_db"
        read_only = false

        [databases.withpw]
        dbname = "secured_db"
        user = "reader"
        password_env = "PW"
        read_only = true
        """
    )
    monkeypatch.setenv("POSTGRES_MCP_CONFIG", str(path))
    return path


@pytest.fixture
def fresh_server(config_file: Path):  # type: ignore[no-untyped-def]
    """Import server with its config cache cleared."""
    import server

    server._config = None
    server._force_read_only = False
    return server
