# postgres-mcp

An MCP server that queries PostgreSQL through the `psql` command-line client.
Configure as many databases as you like; mark any of them read-only and the
model cannot talk its way out of it.

## Requirements

- Python 3.11 or newer
- `psql` on your PATH

Check with `psql --version`. If it is missing:

| Platform | Command |
|---|---|
| macOS, client only | `brew install libpq && brew link --force libpq` |
| macOS, full server | `brew install postgresql@18` |
| Debian, Ubuntu | `sudo apt install postgresql-client` |
| RHEL, Fedora | `sudo dnf install postgresql` |
| Windows | `scoop install postgresql`, or the EDB installer |

`brew install libpq` is keg-only: without `brew link --force`, psql installs
but stays off your PATH. `test_connection` detects that case and prints the
`export PATH=...` line you need. You can also point the server at a specific
binary with `POSTGRES_MCP_PSQL=/path/to/psql`.

## Install

```bash
git clone <this repo> ~/workspace/postgres-mcp
cd ~/workspace/postgres-mcp
uv venv && uv pip install -e ".[dev]"
uv run pytest
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
password_env = "PROD_PG_PASSWORD"   # env var NAME, never the secret
read_only = true
```

Each entry takes either `dsn` or the discrete fields, not both. Passwords never
go in this file: `password_env` names an environment variable, or use
`~/.pgpass`.

Run `test_connection` with no arguments to validate the whole file at once.

## Register with an MCP client

```json
{
  "mcpServers": {
    "postgres": {
      "command": "uv",
      "args": [
        "run", "--directory", "/absolute/path/to/postgres-mcp",
        "postgres-mcp", "--read-only"
      ],
      "env": { "PROD_PG_PASSWORD": "..." }
    }
  }
}
```

`--read-only` forces every configured database read-only regardless of the
config file. `POSTGRES_MCP_READ_ONLY=1` does the same.

## Tools

| Tool | Purpose |
|---|---|
| `list_databases` | Configured databases and their read-only status |
| `execute_sql` | Run SQL, returns CSV |
| `describe_schema` | Tables in a schema, or one table's columns and indexes |
| `test_connection` | Verify psql and connectivity |

## How read-only is enforced

Three independent layers:

1. **psql meta-commands are always rejected.** `\!` runs shell commands and
   `\copy` writes local files, so any statement opening with a backslash is
   refused in both modes.
2. **A statement gate** parses your SQL, ignoring comments and string
   literals, and requires every statement to open with `SELECT`, `WITH`,
   `EXPLAIN`, `SHOW`, `TABLE` or `VALUES` and to contain no `INSERT`,
   `UPDATE`, `DELETE`, `MERGE` or `INTO` (`SELECT ... INTO` creates a table).
   `WITH x AS (...) INSERT ...` is caught.
   `EXPLAIN ANALYZE SELECT` is allowed; `EXPLAIN ANALYZE UPDATE` is not.
3. **`BEGIN READ ONLY`** wraps the statements, so the server itself refuses
   writes even if the gate were fooled.

No tool takes a `read_only` argument, so the model cannot escalate its own
privileges: a prompt-injection payload in a web page or a table comment has no
switch to flip.

### Recommended: a read-only role

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

- `SET` is rejected in read-only mode, so `search_path` cannot be changed.
  Schema-qualify your tables, or use `describe_schema`.
- `SELECT ... FOR UPDATE` and `FOR SHARE` are rejected in read-only mode: they
  take row locks, which a read-only transaction refuses anyway.
- `max_rows` truncates output after the query has run. No `LIMIT` is injected
  into your SQL, so the reported total is the real one — add your own `LIMIT`
  if the query itself is expensive.
- NULL and the empty string are not distinguished in query output: psql writes
  NULL as an unquoted empty CSV field and a zero-length string as a quoted
  one, but `results.py` parses with Python's `csv` module for correct row
  counting, and `csv.reader` reports both as `''`. This is an accepted
  limitation recorded in the design spec's open risks; `psql
  --pset=null=<marker>` is the likely fix once a live database is available
  to verify it against.
