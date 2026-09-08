"""Locating and running the psql binary."""
from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Mapping

# Places psql commonly lives when it is installed but not on PATH. The libpq
# entries matter most: `brew install libpq` is keg-only and does not link psql.
PROBE_PATHS: tuple[str, ...] = (
    "/opt/homebrew/opt/libpq/bin/psql",
    "/usr/local/opt/libpq/bin/psql",
    "/opt/homebrew/bin/psql",
    "/usr/local/bin/psql",
    "/usr/bin/psql",
    "/Applications/Postgres.app/Contents/Versions/latest/bin/psql",
)


class PsqlNotFound(Exception):
    """Raised when no usable psql binary can be located."""

    def __init__(self, guidance: str) -> None:
        super().__init__(guidance)
        self.guidance = guidance


def find_psql(
    env: Mapping[str, str] | None = None, *, platform_name: str | None = None
) -> str:
    """Return a path to psql, or raise PsqlNotFound carrying install guidance."""
    environ = os.environ if env is None else env
    platform = sys.platform if platform_name is None else platform_name

    override = environ.get("POSTGRES_MCP_PSQL")
    if override:
        return override

    found = shutil.which("psql")
    if found:
        return found

    for candidate in PROBE_PATHS:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            raise PsqlNotFound(install_guidance(platform, found_at=candidate))

    raise PsqlNotFound(install_guidance(platform))


def install_guidance(platform_name: str, found_at: str | None = None) -> str:
    """Return actionable text for a missing or unreachable psql."""
    if found_at is not None:
        directory = os.path.dirname(found_at)
        return (
            f"psql is installed at {found_at} but not on your PATH.\n"
            f"Add it with:\n\n    export PATH=\"{directory}:$PATH\"\n\n"
            "Add that line to your shell profile to make it permanent, or set "
            f"POSTGRES_MCP_PSQL={found_at} for this server only."
        )

    if platform_name == "darwin":
        return (
            "psql was not found. Install it with one of:\n\n"
            "    brew install libpq && brew link --force libpq   # client only\n"
            "    brew install postgresql@18                      # full server\n\n"
            "Note that libpq is keg-only: without `brew link --force`, psql is "
            "installed but stays off your PATH."
        )

    if platform_name.startswith("win"):
        return (
            "psql was not found. Install it with:\n\n"
            "    scoop install postgresql\n\n"
            "or use the EDB installer from "
            "https://www.postgresql.org/download/windows/"
        )

    return (
        "psql was not found. Install it with whichever fits your distribution:\n\n"
        "    sudo apt install postgresql-client   # Debian, Ubuntu\n"
        "    sudo dnf install postgresql          # RHEL, Fedora\n"
        "    sudo pacman -S postgresql-libs       # Arch"
    )
