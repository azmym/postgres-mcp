"""Locating and running the psql binary."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass

from postgres_mcp import guard
from postgres_mcp.config import Database

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


_CONNECT_TIMEOUT_SECONDS = "10"


@dataclass(frozen=True)
class PsqlResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int


class GuardRejected(Exception):
    """Raised when the statement gate refuses the SQL."""

    def __init__(self, result: guard.GuardResult) -> None:
        super().__init__(result.reason or "statement rejected")
        self.result = result


def build_argv(
    psql_path: str,
    db: Database,
    *,
    read_only: bool,
    variables: Mapping[str, str] | None = None,
) -> list[str]:
    """Build the psql command line. Never includes a password."""
    argv = [
        psql_path,
        "--no-psqlrc",  # a user's ~/.psqlrc must not change output format
        "--csv",
        # Without a marker, psql renders both NULL and the empty string as a
        # bare empty CSV field, so the two are indistinguishable downstream.
        # With one, NULL is instead indistinguishable from a column whose
        # literal text is "[NULL]". That second collision is far rarer than the
        # first, which is why the marker wins — but it is a trade, not a free
        # improvement: the ambiguity is moved, not removed.
        "--pset=null=[NULL]",
        # Quiet is mandatory: without it psql echoes a command tag (SET, BEGIN,
        # ROLLBACK) for each statement, and render_csv parses those tags as
        # data rows, corrupting the header and inflating the row count.
        "-q",
        "-v",
        "ON_ERROR_STOP=1",
    ]

    if not read_only:
        # Read-only mode gets atomicity from its explicit transaction wrapper.
        argv.append("--single-transaction")

    for key, value in (variables or {}).items():
        argv += ["-v", f"{key}={value}"]

    if db.dsn:
        argv.append(db.dsn)
        return argv

    if db.host:
        argv += ["-h", db.host]
    if db.port:
        argv += ["-p", str(db.port)]
    if db.user:
        argv += ["-U", db.user]
    if db.dbname:
        argv += ["-d", db.dbname]

    return argv


def build_env(db: Database, base_env: Mapping[str, str]) -> dict[str, str]:
    """Build the child environment, carrying the password out of argv's reach."""
    env = dict(base_env)
    env["PGCONNECT_TIMEOUT"] = _CONNECT_TIMEOUT_SECONDS

    if db.password_env:
        password = base_env.get(db.password_env)
        if password:
            env["PGPASSWORD"] = password
    if db.sslmode:
        env["PGSSLMODE"] = db.sslmode

    return env


def build_input(sql: str, db: Database, *, read_only: bool) -> str:
    """Build psql's stdin: timeout, optional read-only wrapper, then the SQL."""
    body = sql.strip()
    # Decide termination from the scrubbed body: a semicolon inside a trailing
    # comment is not a terminator. The text sent to psql stays the original.
    terminated = guard.scrub(body).strip().endswith(";")

    lines = [f"SET statement_timeout = '{db.statement_timeout}';"]
    if read_only:
        lines += ["BEGIN READ ONLY;", body]
        if not terminated:
            # Terminate on its own line so a trailing line comment cannot
            # swallow the semicolon into the comment.
            lines.append(";")
        lines.append("ROLLBACK;")
    else:
        lines.append(body)
        if not terminated:
            lines.append(";")

    return "\n".join(lines) + "\n"


def run_sql(
    db: Database,
    sql: str,
    *,
    read_only: bool,
    variables: Mapping[str, str] | None = None,
    env: Mapping[str, str] | None = None,
    psql_path: str | None = None,
    timeout: float = 60.0,
) -> PsqlResult:
    """Gate the SQL, then run it through psql and return the raw result."""
    verdict = guard.check(sql, read_only=read_only)
    if not verdict.allowed:
        raise GuardRejected(verdict)

    base_env = os.environ if env is None else env
    binary = psql_path or find_psql(base_env)

    try:
        completed = subprocess.run(
            build_argv(binary, db, read_only=read_only, variables=variables),
            input=build_input(sql, db, read_only=read_only),
            env=build_env(db, base_env),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        # TimeoutExpired is a SubprocessError, not an OSError, so callers
        # cannot catch it alongside ordinary spawn failures. Report it as a
        # failed run instead of letting it escape as an unhandled exception.
        return PsqlResult(
            ok=False,
            stdout="",
            stderr=(
                f"psql did not finish within {timeout:g}s and was terminated. "
                f"The server-side statement_timeout is "
                f"{db.statement_timeout}; a hang before that usually means the "
                "connection itself is stalling."
            ),
            returncode=-1,
        )

    return PsqlResult(
        ok=completed.returncode == 0,
        stdout=completed.stdout,
        stderr=completed.stderr,
        returncode=completed.returncode,
    )
