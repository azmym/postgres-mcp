"""Tests for locating psql and for the install guidance text."""
from __future__ import annotations

import pytest

from postgres_mcp import psql


def test_env_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(psql.shutil, "which", lambda _: "/usr/bin/psql")
    env = {"POSTGRES_MCP_PSQL": "/custom/psql"}

    assert psql.find_psql(env) == "/custom/psql"


def test_uses_path_lookup_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(psql.shutil, "which", lambda _: "/opt/homebrew/bin/psql")

    assert psql.find_psql({}) == "/opt/homebrew/bin/psql"


def test_falls_back_to_probe_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """brew install libpq is keg-only, so psql exists but is not on PATH."""
    probe = "/opt/homebrew/opt/libpq/bin/psql"
    monkeypatch.setattr(psql.shutil, "which", lambda _: None)
    monkeypatch.setattr(psql.os.path, "isfile", lambda p: p == probe)
    monkeypatch.setattr(psql.os, "access", lambda p, mode: p == probe)

    with pytest.raises(psql.PsqlNotFound) as excinfo:
        psql.find_psql({}, platform_name="darwin")

    assert probe in excinfo.value.guidance
    assert "PATH" in excinfo.value.guidance


def test_raises_with_macos_guidance_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(psql.shutil, "which", lambda _: None)
    monkeypatch.setattr(psql.os.path, "isfile", lambda _: False)

    with pytest.raises(psql.PsqlNotFound) as excinfo:
        psql.find_psql({}, platform_name="darwin")

    assert "brew install libpq" in excinfo.value.guidance


def test_linux_guidance_mentions_apt_and_dnf() -> None:
    guidance = psql.install_guidance("linux")

    assert "apt install postgresql-client" in guidance
    assert "dnf install postgresql" in guidance


def test_windows_guidance_mentions_scoop() -> None:
    assert "scoop install postgresql" in psql.install_guidance("win32")


def test_found_at_guidance_explains_path_problem() -> None:
    guidance = psql.install_guidance("darwin", found_at="/opt/libpq/bin/psql")

    assert "/opt/libpq/bin/psql" in guidance
    assert "not on your PATH" in guidance
