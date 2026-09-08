"""postgres-mcp server.

Exposes PostgreSQL databases as MCP tools, driven through the psql CLI.
Run with: uvx --from "fastmcp[cli]" fastmcp run server.py
"""
from __future__ import annotations

import argparse

from fastmcp import FastMCP

from postgres_mcp import config as config_mod
from postgres_mcp import psql, results

mcp = FastMCP("postgres")

_config: config_mod.Config | None = None
_force_read_only: bool = False


def _get_config() -> config_mod.Config:
    """Load and cache the config, honouring the global read-only flag."""
    global _config
    if _config is None:
        _config = config_mod.load_config(force_read_only=_force_read_only)
    return _config


@mcp.tool()
def list_databases() -> dict[str, object]:
    """List the configured PostgreSQL databases and their read-only status.

    Returns each database's name, connection target and whether it accepts
    writes. Passwords are never included.
    """
    try:
        config = _get_config()
    except config_mod.ConfigError as exc:
        return {"error": str(exc), "databases": []}

    entries = [
        {
            "name": db.name,
            "host": db.host or ("(from dsn)" if db.dsn else "localhost"),
            "dbname": db.dbname or "(from dsn)",
            "user": db.user or "(default)",
            "read_only": db.read_only,
            "max_rows": db.max_rows,
        }
        for db in config.databases.values()
    ]
    return {"databases": entries, "force_read_only": config.force_read_only}


@mcp.tool()
def execute_sql(database: str, query: str, max_rows: int | None = None) -> str:
    """Run SQL against a configured database and return the rows as CSV.

    `database` is a name from list_databases. Whether writes are permitted is
    fixed by configuration and cannot be changed per call: against a read-only
    database, only read queries run.
    """
    try:
        db = _get_config().get(database)
    except config_mod.ConfigError as exc:
        return f"Error: {exc}"

    try:
        outcome = psql.run_sql(db, query, read_only=db.read_only)
    except psql.GuardRejected as exc:
        return (
            f"Rejected: {exc.result.reason}\n\n"
            f"Statement: {exc.result.statement}"
        )
    except psql.PsqlNotFound as exc:
        return exc.guidance
    except OSError as exc:
        return f"Error running psql: {exc}"

    if not outcome.ok:
        return f"psql failed (exit {outcome.returncode}):\n{outcome.stderr.strip()}"

    rendered = results.render_csv(
        outcome.stdout, max_rows=max_rows or db.max_rows
    )
    if not rendered.text:
        return "(no rows)"
    return rendered.text


def main() -> None:
    """Entry point. --read-only forces every database read-only."""
    global _force_read_only

    parser = argparse.ArgumentParser(prog="postgres-mcp")
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="force every configured database read-only, overriding the config",
    )
    args = parser.parse_args()
    _force_read_only = args.read_only

    mcp.run()


if __name__ == "__main__":
    main()
