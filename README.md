<div align="center">

# postgres-mcp

An MCP server that lets an AI assistant run SQL against PostgreSQL through the
`psql` command-line client.

[![CI](https://github.com/azmym/postgres-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/azmym/postgres-mcp/actions/workflows/ci.yml)
![Python 3.11 | 3.12 | 3.13](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

</div>

You configure several databases at once, and any of them can be marked
read-only so that only read queries ever run against it.

It exists for one reason: when you hand a model a database, you want the
read-only guarantee to come from the server and from PostgreSQL itself, not
from the model's good behaviour. A prompt-injection payload in a web page or a
table comment cannot talk its way into a write.

![The four-layer read-only defence, left to right: an AI assistant's query
first meets layer one, a meta-command ban that blocks psql backslash commands
such as \! to prevent shell execution and file access; then layer two, a
statement gate validating SQL against an allowlist of openers including SELECT,
WITH, EXPLAIN, TABLE and VALUES; then layer three, where the query is wrapped
in a BEGIN READ ONLY transaction so PostgreSQL itself rejects any write; and
finally layer four, a read-only database role granting only SELECT, configured
in the database rather than in this server. Underneath, a zero-escalation note:
read-only status is fixed in configuration, so the model cannot escalate its
own privileges.](assets/infographic.png)

## Requirements

- Python 3.11 or newer
- `psql` on your PATH

`fastmcp` is the only runtime dependency; everything else is the standard
library.

Check for psql with `psql --version`. If it is missing:

| Platform | Command |
|---|---|
| macOS, client only | `brew install libpq && brew link --force libpq` |
| macOS, full server | `brew install postgresql@18` |
| Debian, Ubuntu | `sudo apt install postgresql-client` |
| RHEL, Fedora | `sudo dnf install postgresql` |
| Arch | `sudo pacman -S postgresql-libs` |
| Windows | `scoop install postgresql`, or the EDB installer |

`brew install libpq` is keg-only: without `brew link --force`, psql installs but
stays off your PATH. `test_connection` detects that case and prints the
`export PATH=...` line you need. You can also point the server at a specific
binary with `POSTGRES_MCP_PSQL=/path/to/psql`.

## Install

```bash
git clone https://github.com/azmym/postgres-mcp
cd postgres-mcp
uv venv && uv pip install -e ".[dev]"
```

## Configure

Create `~/.config/postgres-mcp/config.toml` (override the location with
`POSTGRES_MCP_CONFIG`):

```toml
[defaults]
read_only = true          # safe by default
statement_timeout = "30s"
max_rows = 1000

[databases.local]
dsn = "postgresql://me@localhost:5432/appdb"
read_only = false

[databases.prod]
host = "prod.example.com"
port = 5432
user = "readonly_svc"
dbname = "app"
sslmode = "require"
password_env = "PROD_PG_PASSWORD"   # the env var NAME, never the secret
read_only = true
```

Each entry takes either `dsn` or the discrete fields (`host`, `port`, `user`,
`dbname`, `sslmode`), not both. Passwords never go in this file: `password_env`
names an environment variable, and `~/.pgpass` covers the rest. The password
travels to psql in the child process's `PGPASSWORD` environment variable, never
on the command line, because argv is visible to `ps`.

Run `test_connection` with no arguments to validate the whole file at once.

## Register with an MCP client
Directly from GitHub repo 
```json
{
  "mcpServers": {
    "postgres": {
      "command": "uvx",
      "args": [
        "--from", "git+https://github.com/azmym/postgres-mcp@v0.1.0",
        "postgres-mcp", "--read-only"
      ],
      "env": { "PROD_PG_PASSWORD": "..." }
    }
  }
}
```
Or refer to your local directory 
```json
{
  "mcpServers": {
    "postgres": {
      "command": "uv",
      "args": [
        "run", "--directory", "/absolute/path/to/postgres-mcp",
        "postgres-mcp", "--read-only"
      ],
      "env": {
        "POSTGRES_MCP_CONFIG": "/absolute/path/to/config.toml",
        "PROD_PG_PASSWORD": "..."
      }
    }
  }
}
```

`--read-only` forces every configured database read-only regardless of the
config file. `POSTGRES_MCP_READ_ONLY=1` does the same.

The `env` block needs one entry for every `password_env` your config declares.
The example above shows one because the example config has one: `local`
connects with a dsn and no password, and only `prod` names a variable. A config
with four databases needs four entries, one per variable name.

Writing the password here puts it in a second file on disk, which is what
`password_env` was meant to avoid. Two ways around that: put the credentials in
`~/.pgpass`, which psql reads without any variable, or export the variable in
the shell that launches your MCP client and leave it out of the JSON. The
server only needs the variable to exist in its environment; it does not care
who set it.

`POSTGRES_MCP_CONFIG` points the server at a specific config file, which is what
lets you run more than one instance. See below.

## Several environments

Say you run MSS on both staging and production. You have two ways to set that
up, and they differ in how much separation you get.

### One config file

Put both databases in `~/.config/postgres-mcp/config.toml` and tell them apart
by name:

```toml
[defaults]
read_only = true          # anything you forget to mark stays read-only

[databases.mss_staging]
host = "mss-staging.internal"
port = 5432
user = "app"
dbname = "mss"
password_env = "MSS_STAGING_PW"
read_only = false

[databases.mss_production]
host = "mss-prod.internal"
port = 5432
user = "readonly_svc"
dbname = "mss"
sslmode = "require"
password_env = "MSS_PROD_PW"
read_only = true
statement_timeout = "10s"
max_rows = 200
```

You manage one file and one client entry. Each database keeps its own mode and
limits, so a write against `mss_staging` goes through while the gate refuses the
same statement against `mss_production`, which also runs on a shorter timeout and
a smaller row cap.

The assistant sees both databases in one list and picks between them by name.
Read-only on production stops a wrong pick from damaging data, but it will still
return production rows into a conversation you meant to keep on staging, and
nothing enforces your naming convention.

### Two server instances

Split the databases across two config files, `staging.toml` and `production.toml`,
each holding one entry, then register both:

```json
{
  "mcpServers": {
    "mss-staging": {
      "command": "uvx",
      "args": [
        "--from", "git+https://github.com/azmym/postgres-mcp@v0.1.0",
        "postgres-mcp"
      ],
      "env": {
        "POSTGRES_MCP_CONFIG": "/Users/you/.config/postgres-mcp/staging.toml",
        "MSS_STAGING_PW": "..."
      }
    },
    "mss-production": {
      "command": "uvx",
      "args": [
        "--from", "git+https://github.com/azmym/postgres-mcp@v0.1.0",
        "postgres-mcp", "--read-only"
      ],
      "env": {
        "POSTGRES_MCP_CONFIG": "/Users/you/.config/postgres-mcp/production.toml",
        "MSS_PROD_PW": "..."
      }
    }
  }
}
```

The environment becomes part of the tool name, so the assistant selects
`mss-production` as its own tool rather than pulling a string from a list. The
production instance also carries `--read-only` at the process level, where a
mistake in the config file cannot reach it, and its password lives only in that
process's environment, so a staging session never holds it.

You pay two files and two client entries, and you lose the ability to query
staging and production in one call.

### Choosing

Use one file while you work on your own machine against data you can afford to
break. Move to two instances once production data is in reach, where the cost of
the assistant picking the wrong name outweighs the cost of a second config file.

Either way, create a read-only role on production and point that entry's `user`
at it. It holds even if this server has a bug, and the SQL is below.

Both approaches take more than two databases. Adding MAS alongside MSS gives you
four entries in one file, or two files of two entries each if you split them by
environment.

## Tools

| Tool | Purpose |
|---|---|
| `list_databases` | Configured databases and their read-only status |
| `execute_sql` | Run SQL, returns CSV |
| `describe_schema` | Tables in a schema, or one table's columns, constraints and indexes |
| `test_connection` | Check psql and connectivity; reports the psql binary, server version, connected user, and each database's mode |

`describe_schema` is always read-only, even against a writable database.

## How read-only is enforced

Three independent layers, plus a fourth you should add yourself. The diagram
above counts all four; this one shows what the three enforced layers check,
including the keywords each one permits and blocks.

![The three enforced layers stacked as a shield. Layer one, the psql
meta-command ban, blocks backslash commands to prevent shell execution and
unauthorized local file access. Layer two, the statement gate, enforces a
keyword allowlist of SELECT, WITH, EXPLAIN and SHOW while scanning for hidden
write verbs, listing INSERT, UPDATE, DELETE, DROP, MERGE, INTO, SELECT INTO and
EXPLAIN ANALYZE UPDATE as blocked. Layer three, the PostgreSQL transaction
lock, wraps all queries in BEGIN READ ONLY so refusal happens at the database.
Alongside: zero privilege escalation, because tools do not accept read-only
arguments, and a recommended restricted SQL role as a final layer independent
of this server's code.](assets/Securing_SQL_Databases_for_AI.png)

1. **Backslash commands are always rejected.** `\!` runs a shell command on
   this host and never reaches the server, and `\copy` and `\o` write local
   files, so any statement opening with a backslash is refused in both modes.
2. **A statement gate** scrubs comments, string literals, quoted identifiers
   and dollar-quoted bodies, then requires every statement to open with one of
   six keywords: `SELECT`, `WITH`, `EXPLAIN`, `SHOW`, `TABLE` or `VALUES`. It
   then scans for the write verbs `INSERT`, `UPDATE`, `DELETE`, `MERGE` and
   `INTO` (`SELECT ... INTO` creates a table), so `WITH x AS (...) INSERT ...`
   is caught even though it opens with an allowed keyword. `EXPLAIN ANALYZE
   SELECT` is allowed; `EXPLAIN ANALYZE UPDATE` is not, because `ANALYZE`
   executes its argument. `SELECT ... FOR UPDATE` and `FOR SHARE` are rejected
   too, since they take row locks a read-only transaction refuses.
3. **`BEGIN READ ONLY`** wraps the statements, so PostgreSQL itself refuses a
   write even if the gate were fooled.

No tool takes a `read_only` argument, so the model cannot escalate its own
privileges. The switch lives in configuration, not in the request.

Read-only precedence, highest first: the `--read-only` flag or
`POSTGRES_MCP_READ_ONLY=1`, then a per-database `read_only`, then
`[defaults].read_only`, then a built-in default of `true`. The global switch
can only tighten, never loosen.

### A read-only role (recommended)

The only layer that does not depend on this server's correctness is the
database's own permissions:

```sql
CREATE ROLE mcp_reader LOGIN PASSWORD 'choose-something-strong';
GRANT CONNECT ON DATABASE app TO mcp_reader;
GRANT USAGE ON SCHEMA public TO mcp_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO mcp_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT ON TABLES TO mcp_reader;
```

Point `prod.user` at `mcp_reader` and a bug in this server still cannot write.

## Known limits

- `SET` is not an allowed opener in read-only mode, so `search_path` cannot be
  changed. Schema-qualify your tables, or use `describe_schema`.
- Output is CSV, truncated by `max_rows` (default 1000) with a trailer naming
  the real total, plus a 100 KB byte ceiling. No `LIMIT` is injected into your
  SQL, so the reported total is the real one; add your own `LIMIT` if the query
  itself is expensive.
- NULL renders as `[NULL]` to distinguish it from an empty string. A column
  whose literal text is `[NULL]` is therefore ambiguous with a real NULL.

## Tests

`tests/test_integration.py` runs against a real PostgreSQL server. When none is
reachable it skips rather than failing. Point it at a server with the standard
libpq variables:

```bash
PGHOST=localhost PGUSER=admin PGPASSWORD=admin uv run pytest
```

Or spin up a throwaway server with Docker:

```bash
docker run --rm -d --name pg-mcp-test \
  -e POSTGRES_PASSWORD=admin -p 5432:5432 postgres:18
PGHOST=localhost PGUSER=postgres PGPASSWORD=admin uv run pytest
docker rm -f pg-mcp-test
```

Without a server the integration tests skip and the unit tests still run
(151 passed, 10 skipped). Against a live server all 161 pass. Verified against
PostgreSQL 18.6.

## Troubleshooting

The first failure you will hit is psql not being found. The server looks on
`PATH`, then probes common install locations, and raises a specific message for
each case. On macOS the usual cause is `brew install libpq` without
`brew link --force`, which installs psql but leaves it off your PATH; the error
tells you the exact `export PATH=...` line to add. Set `POSTGRES_MCP_PSQL` to
skip the search.

## Contributing

[CONTRIBUTING.md](CONTRIBUTING.md) covers the setup, the two test modes, and
the conventions the code follows. The project is MIT-licensed
([LICENSE](LICENSE)).

Security is the whole point of this server, so [SECURITY.md](SECURITY.md) is
worth reading: it documents the threat model, what counts as a vulnerability,
and how to report one privately.
