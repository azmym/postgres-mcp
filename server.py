"""postgres-mcp server.

Exposes PostgreSQL databases as MCP tools, driven through the psql CLI.
Run with: uvx --from "fastmcp[cli]" fastmcp run server.py

`fastmcp run` calls mcp.run() directly and never reaches main(), so the
--read-only flag is not parsed on that path. To force read-only there, set
POSTGRES_MCP_READ_ONLY=1 instead.
"""
from __future__ import annotations

import argparse
import re

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

    if max_rows is not None and max_rows < 1:
        return f"Error: max_rows must be at least 1, got {max_rows}"

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
        outcome.stdout, max_rows=db.max_rows if max_rows is None else max_rows
    )
    if not rendered.text:
        return "(no rows)"
    return rendered.text


# Identifiers reach psql as variables, but they are still validated so a
# malformed name fails with a clear message instead of a catalog error.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

_TABLE_LIST_SQL = """
-- Aliases that are reserved words are double-quoted: bare `AS table`,
-- `AS column` and `AS default` are syntax errors in PostgreSQL. `type` is
-- non-reserved, so `AS type` is valid SQL; quoting it here is defensive
-- consistency with the other aliases, not a requirement.
SELECT c.relname AS "table",
       CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view'
                      WHEN 'm' THEN 'matview' WHEN 'p' THEN 'partitioned'
                      ELSE c.relkind::text END AS kind,
       count(a.attname) AS columns
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_attribute a
       ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
WHERE n.nspname = :'schema' AND c.relkind IN ('r', 'v', 'm', 'p')
GROUP BY c.relname, c.relkind
ORDER BY c.relname
"""

_COLUMNS_SQL = """
SELECT a.attname AS "column",
       format_type(a.atttypid, a.atttypmod) AS "type",
       NOT a.attnotnull AS nullable,
       pg_get_expr(d.adbin, d.adrelid) AS "default"
FROM pg_attribute a
LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE a.attrelid = (quote_ident(:'schema') || '.' || quote_ident(:'table'))::regclass
  AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum
"""

_CONSTRAINTS_SQL = """
SELECT conname AS name, pg_get_constraintdef(oid) AS definition
FROM pg_constraint
WHERE conrelid = (quote_ident(:'schema') || '.' || quote_ident(:'table'))::regclass
ORDER BY contype, conname
"""

_INDEXES_SQL = """
SELECT indexname AS name, indexdef AS definition
FROM pg_indexes
WHERE schemaname = :'schema' AND tablename = :'table'
ORDER BY indexname
"""

_CONNECTION_SQL = """
SELECT version() AS version,
       current_user AS username
"""


def _introspect(db: config_mod.Database, sql: str, variables: dict[str, str]) -> str:
    """Run one introspection query read-only and render it, or return an error."""
    try:
        outcome = psql.run_sql(db, sql, read_only=True, variables=variables)
    except psql.PsqlNotFound as exc:
        return exc.guidance
    except (psql.GuardRejected, OSError) as exc:
        return f"Error: {exc}"

    if not outcome.ok:
        return outcome.stderr.strip()

    rendered = results.render_csv(outcome.stdout, max_rows=db.max_rows)
    return rendered.text or "(none)"


@mcp.tool()
def describe_schema(
    database: str, schema: str = "public", table: str | None = None
) -> str:
    """Describe a schema's tables, or one table's columns and indexes.

    Without `table`, lists the schema's tables, views and column counts. With
    `table`, returns that table's columns, constraints and indexes. Always
    read-only, even against a writable database.
    """
    try:
        db = _get_config().get(database)
    except config_mod.ConfigError as exc:
        return f"Error: {exc}"

    for label, value in (("schema", schema), ("table", table)):
        if value is not None and not _IDENTIFIER_RE.match(value):
            return (
                f"Error: invalid {label} name {value!r}; expected a plain "
                "identifier such as public or users"
            )

    if table is None:
        listing = _introspect(db, _TABLE_LIST_SQL, {"schema": schema})
        return f"Tables in {schema}:\n{listing}"

    variables = {"schema": schema, "table": table}
    sections = (
        ("Columns", _COLUMNS_SQL),
        ("Constraints", _CONSTRAINTS_SQL),
        ("Indexes", _INDEXES_SQL),
    )
    parts = [f"{schema}.{table}"]
    for heading, sql in sections:
        parts.append(f"\n{heading}:\n{_introspect(db, sql, variables)}")

    return "\n".join(parts)


@mcp.tool()
def test_connection(database: str | None = None) -> str:
    """Check psql and connectivity, for one database or all of them.

    Reports the psql binary and version, the server version, the connected
    user, and each database's configured read-only or read-write mode. Omit
    `database` to check every configured entry, which also validates the
    config file.
    """
    try:
        config = _get_config()
    except config_mod.ConfigError as exc:
        return f"Error: {exc}"

    try:
        binary = psql.find_psql()
    except psql.PsqlNotFound as exc:
        return exc.guidance

    if database is None:
        targets = list(config.databases.values())
    else:
        try:
            targets = [config.get(database)]
        except config_mod.ConfigError as exc:
            return f"Error: {exc}"

    lines = [f"psql: {binary}"]
    for db in targets:
        mode = "read-only" if db.read_only else "read-write"
        lines.append(f"\n{db.name} ({mode}):")
        lines.append(_introspect(db, _CONNECTION_SQL, {}))

    return "\n".join(lines)


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
