# postgres-mcp — Design

Date: 2026-09-08
Status: Approved, not yet implemented

## Purpose

An MCP server that lets an LLM query PostgreSQL databases through the `psql`
command-line client. It exists to make ad-hoc database exploration safe: many
configured databases side by side, with read-only enforcement that the model
cannot switch off.

Two requirements drive every decision below:

1. A user configures more than one database and picks one per call.
2. A database can be marked read-only, and read-only means only read queries run.

## Non-goals

- No connection pooling, no persistent sessions. Each call is one `psql` process.
- No ORM, no migration running, no schema authoring.
- No Python database driver. `psql` is the transport, per the original request.
- No write-path conveniences (bulk insert helpers, `COPY FROM` wrappers).

## Stack

Python 3.11+ with FastMCP, matching the sibling `gemini-mcp` project:
`hatchling` build backend, `uv` for dev, `pytest` for tests. `fastmcp` is the
only runtime dependency; TOML parsing uses stdlib `tomllib`.

## Configuration

Config lives at `~/.config/postgres-mcp/config.toml`. The path is overridable
with `POSTGRES_MCP_CONFIG`.

```toml
[defaults]
read_only = true          # safe by default
statement_timeout = "30s"
max_rows = 1000

[databases.local]
dsn = "postgresql://mahmoud@localhost:5432/appdb"
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

### Rules

- Each `[databases.<name>]` entry supplies either `dsn` or the discrete fields
  (`host`, `port`, `user`, `dbname`, `sslmode`). Supplying both, or neither, is
  a configuration error that names the offending entry.
- Secrets never appear in the config file. `password_env` names an environment
  variable holding the password; absent that, `~/.pgpass` applies as usual.
  If `password_env` names a variable that is unset, the entry fails validation
  with a message naming both the entry and the variable.
- `[defaults]` supplies `read_only`, `statement_timeout` and `max_rows` for
  entries that omit them.

### Read-only precedence

Highest to lowest:

1. `--read-only` CLI flag, or `POSTGRES_MCP_READ_ONLY=1`
2. per-database `read_only`
3. `[defaults].read_only`
4. built-in default: `true`

The global flag can only tighten. `--read-only` against a config containing
`read_only = false` yields a read-only server. Nothing loosens a read-only
setting at runtime.

No tool accepts a `read_only` argument. The model cannot escalate its own
privileges, so a prompt-injection payload in a web page, a table comment or a
column default cannot talk the model into a write.

## Security model

`psql` has its own meta-language on top of SQL, which is the main hazard of a
CLI-based design: `\!` runs a shell command, `\copy` and `\o` write local
files. Three independent layers address this.

### Layer 1 — meta-command ban (all modes, always)

Any statement whose first non-whitespace character is `\` is rejected before
`psql` is invoked, in read-write mode as well as read-only. Backslash commands
have no legitimate use here, and permitting them converts SQL execution into
arbitrary command execution.

### Layer 2 — statement gate (read-only mode)

A tokenizer strips comments (`--`, `/* */`), single-quoted strings,
double-quoted identifiers and dollar-quoted bodies (`$$ ... $$`,
`$tag$ ... $tag$`), then splits the input on top-level semicolons.

The gate inspects the user's SQL only. The wrapper this server adds in layer 3
is constructed after the gate runs and is never subject to it.

Each resulting statement must satisfy two conditions.

**Condition 1 — leading keyword allowlist.** The statement leads with one of
`SELECT`, `WITH`, `EXPLAIN`, `SHOW`, `TABLE`, `VALUES`. Anything else is
rejected, which already covers `INSERT`, `UPDATE`, `DELETE`, `CREATE`, `ALTER`,
`DROP`, `TRUNCATE`, `GRANT`, `REVOKE`, `COPY`, `CALL`, `DO`, `VACUUM`,
`ANALYZE`, `REINDEX`, `REFRESH`, `SET`, `RESET`, `LOCK`, `LISTEN` and `NOTIFY`
as statement openers. Note the consequence for `SET`: adjusting `search_path`
is not possible in read-only mode, so queries must schema-qualify their tables
or go through `describe_schema`.

**Condition 2 — no data-modifying keyword at top level.** The statement
contains no top-level `INSERT`, `UPDATE`, `DELETE` or `MERGE`. These four are
the ones condition 1 cannot catch, because a CTE can reach them behind an
allowed opener: `WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x` leads with
`WITH` and writes. Since the tokenizer has already removed strings and
dollar-quoted bodies, a `SELECT 'DELETE FROM t'` literal and a function body
mentioning `INSERT` do not trip this. Keyword matching is case-insensitive and
word-boundary anchored, so an `insert_ts` column name is not a match.

This condition also rejects `SELECT ... FOR UPDATE` and `FOR SHARE`. That is
intended: those take row locks and fail inside a read-only transaction anyway,
so refusing them up front produces a clearer message than the server's error.

**`EXPLAIN` is handled recursively.** `EXPLAIN`, including `EXPLAIN ANALYZE`
and `EXPLAIN (ANALYZE, BUFFERS)`, executes the statement it wraps when
`ANALYZE` is present. So the gate strips a leading `EXPLAIN` and its option
list, then re-applies both conditions to the remainder. `EXPLAIN ANALYZE
SELECT ...` is permitted; `EXPLAIN ANALYZE UPDATE ...` is rejected. Treating
bare `ANALYZE` as a blanket forbidden keyword instead would wrongly reject
`EXPLAIN ANALYZE SELECT`, which is a routine read-only diagnostic.

Rejection names the offending statement and states that the server is in
read-only mode for that database.

### Layer 3 — Postgres itself

In read-only mode the statements are wrapped, over stdin:

```sql
SET statement_timeout = '<configured>';
BEGIN READ ONLY;
<statements>
ROLLBACK;
```

A read-only transaction makes the server refuse writes regardless of what
layer 2 concluded. The `ROLLBACK` discards anything a read might have left
behind, such as a temp table.

In read-write mode there is no `READ ONLY` wrapper, but `SET statement_timeout`
still leads the input so a runaway statement cannot hang indefinitely in either
mode. `psql --single-transaction` supplies atomicity so a script that fails
midway rolls back instead of applying half its statements.

### Invocation details

- `--no-psqlrc` — a user's `~/.psqlrc` must not alter output format or session
  settings.
- `-v ON_ERROR_STOP=1` — stop at the first error instead of continuing.
- `--csv` — stable, parseable, compact output with a header row.
- Statements arrive on **stdin**, not `-c`, so the wrapper can be prepended.
- Passwords pass through the `PGPASSWORD` environment variable of the child
  process. Never argv: argv is world-readable via `ps`.
- `PGCONNECT_TIMEOUT` bounds connection attempts so an unreachable host fails
  promptly rather than hanging the call.

### Recommended fourth layer

The README recommends creating a genuinely read-only Postgres role and using it
in the config, since database-level permissions are the only layer that does
not depend on this server's own correctness.

## Tools

Four tools. Anything else is expressible as SQL.

### `list_databases()`

Returns, per configured entry: name, host, dbname, user, effective `read_only`,
and whether the global force flag is active. Never returns passwords or the
values behind `password_env`.

### `execute_sql(database, query, max_rows=None)`

Resolves the entry, applies the guard, invokes `psql`, returns CSV plus a row
count. An unknown `database` returns the list of valid names. There is
deliberately no `read_only` parameter.

### `describe_schema(database, schema="public", table=None)`

Without `table`: the schema's tables with column counts. With `table`: columns,
types, nullability, defaults, primary key, foreign keys, and indexes, from
`information_schema` and `pg_indexes` queries this server owns, so the model
never guesses at catalog shapes. Runs through the read-only path in all cases.

### `test_connection(database=None)`

Reports the `psql` path and version, server version, `current_user`, and the
enforced mode. With `database` omitted it checks every configured entry, which
doubles as configuration validation. A missing `psql` surfaces here.

## psql discovery and install guidance

`shutil.which("psql")` runs at startup and again per call. `POSTGRES_MCP_PSQL`
overrides discovery with an explicit path.

When `which` fails, these locations are probed before declaring psql absent:

- `/opt/homebrew/opt/libpq/bin/psql`
- `/usr/local/opt/libpq/bin/psql`
- `/opt/homebrew/bin/psql`, `/usr/local/bin/psql`
- `/Applications/Postgres.app/Contents/Versions/latest/bin/psql`

A hit produces "found at `<path>` but not on your PATH", with the line to add
to the shell profile. This case is common: `brew install libpq` is keg-only and
does not link `psql` into `PATH`, so the binary exists while `psql` appears
missing.

A genuine miss produces platform-specific guidance:

| Platform | Command |
|---|---|
| macOS, client only | `brew install libpq && brew link --force libpq` |
| macOS, full server | `brew install postgresql@18` |
| Debian/Ubuntu | `sudo apt install postgresql-client` |
| RHEL/Fedora | `sudo dnf install postgresql` |
| Windows | EDB installer, or `scoop install postgresql` |

Tools still register when `psql` is absent; each call returns the guidance
rather than the server failing to start, so the user can see the instructions
through their MCP client.

## Output shaping

CSV, truncated at `max_rows` (default 1000), with an explicit
`-- truncated: showing 1000 of N rows` trailer when truncation occurs. A hard
byte ceiling near 100 KB applies after row truncation, so a `SELECT *` against
a table with wide text columns cannot exhaust the model's context. Errors
return trimmed `psql` stderr, which already identifies the offending token.

Truncation happens on output, in `results.py`. No `LIMIT` is injected into the
user's SQL. Rewriting a query is the kind of cleverness that breaks on the first
`UNION` or existing `LIMIT`, and it would make the reported row count a lie. The
cost is that the server does the full work before discarding rows, so the
truncation trailer names the real total and the user can add their own `LIMIT`.

## Layout

```
~/workspace/postgres-mcp/
  pyproject.toml          # fastmcp runtime dep; pytest dev extra
  README.md
  server.py               # FastMCP tool definitions, thin
  postgres_mcp/
    config.py             # TOML load, validation, precedence
    psql.py               # discovery, argv/env construction, execution
    guard.py              # tokenizer + read-only statement gate
    results.py            # CSV truncation and formatting
  tests/
```

`gemini-mcp` keeps everything in a single `server.py`, which suits a set of
independent API wrappers. This project has four genuinely separable units with
distinct reasons to change, so each gets a module. `server.py` holds tool
definitions only and delegates.

Each module answers its three questions independently:

- `config.py` — turns a TOML file plus environment into validated database
  entries with an effective read-only decision. Depends on nothing else here.
- `guard.py` — given SQL text and a mode, returns permitted or rejected with a
  reason. Pure function over a string; no I/O, no config knowledge.
- `psql.py` — given an entry, a mode and SQL, builds argv and environment and
  runs the process. Depends on `config` types and `guard`.
- `results.py` — given raw CSV and limits, returns display text. Pure.

## Testing

`pytest`, following `gemini-mcp`'s approach of mocking `subprocess.run` and
asserting on what would have been executed.

`guard.py` carries the most risk and gets the most tests:

- `;` inside a string literal does not split a statement
- `--` and `/* */` comments stripped, including a comment containing `DELETE`
- dollar-quoted function body containing `INSERT` does not trip the gate
- `WITH x AS (...) INSERT INTO ...` is rejected in read-only mode
- `EXPLAIN ANALYZE UPDATE ...` is rejected in read-only mode
- `EXPLAIN ANALYZE SELECT ...` and `EXPLAIN (ANALYZE, BUFFERS) SELECT ...` are
  permitted in read-only mode
- `SELECT ... FOR UPDATE` and `FOR SHARE` are rejected in read-only mode
- `SET search_path` is rejected in read-only mode
- `\!`, `\copy`, `\o`, `\i` rejected in both modes
- legitimate multi-statement `SELECT`s are permitted
- keyword matching is case-insensitive and ignores `insert_ts`-style
  identifiers that merely contain a keyword as a substring

`psql.py`:

- password never appears in argv, and does appear in the child environment
- `--no-psqlrc`, `-v ON_ERROR_STOP=1`, `--csv` always present
- read-only mode wraps in `BEGIN READ ONLY ... ROLLBACK`; read-write mode does
  not, and passes `--single-transaction`
- discovery falls back to the probe list and reports the PATH case distinctly

`config.py`:

- precedence order across CLI, env, per-database and defaults
- global flag tightens `read_only = false` to true
- `dsn` together with discrete fields is an error; neither is an error
- `password_env` naming an unset variable is an error

`results.py`:

- truncation trailer appears with correct counts; absent when not truncated
- byte ceiling applies after row truncation

One integration test runs against a live local Postgres and skips
automatically when nothing answers on `localhost:5432`.

## Open risks

- The tokenizer is the load-bearing security component and hand-written.
  Layer 3 is what keeps a tokenizer bug from becoming a data-loss bug, which is
  why the read-only transaction is not treated as optional.
- `results.py` parses psql's CSV with Python's `csv` module rather than passing
  it through as opaque text. Correct row counting requires it: a quoted field
  may contain newlines, so splitting on newlines both mis-reports the total and
  can cut a row in half. Python's `csv` dialect and psql's `--csv` output are
  both RFC 4180, so the exposure is a quoting divergence between them rather
  than a parsing bug of our own.
