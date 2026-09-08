# postgres-mcp Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an MCP server that queries multiple PostgreSQL databases through the `psql` CLI, with read-only enforcement the model cannot switch off.

**Architecture:** A thin `server.py` holds FastMCP tool definitions and delegates to four focused modules: `config.py` (TOML parsing, validation, read-only precedence), `guard.py` (pure statement gate), `psql.py` (binary discovery, argv/env construction, subprocess execution), `results.py` (CSV truncation). Read-only is enforced in three independent layers: a psql meta-command ban, the statement gate, and a server-side `BEGIN READ ONLY` transaction.

**Tech Stack:** Python 3.11+, FastMCP 3.x, stdlib `tomllib`, `subprocess`, pytest. `fastmcp` is the only runtime dependency.

**Spec:** `docs/superpowers/specs/2026-09-08-postgres-mcp-design.md`

## Global Constraints

Every task's requirements implicitly include this section. Values are copied verbatim from the spec.

- Python `>=3.11` (stdlib `tomllib` requires it). `fastmcp>=3.2.0,<4` is the only runtime dependency; `pytest` and `pytest-mock` are dev-only.
- No tool accepts a `read_only` argument. The model must not be able to escalate its own privileges.
- Read-only precedence, highest first: `--read-only` CLI flag or `POSTGRES_MCP_READ_ONLY=1` → per-database `read_only` → `[defaults].read_only` → built-in default `true`. The global flag can only tighten, never loosen.
- Passwords pass through the child process's `PGPASSWORD` environment variable. **Never** in argv — argv is world-readable via `ps`.
- Every psql invocation includes `--no-psqlrc`, `-v ON_ERROR_STOP=1`, and `--csv`.
- `SET statement_timeout` leads the stdin input in **both** read-only and read-write mode.
- Read-only mode wraps statements in `BEGIN READ ONLY; ... ROLLBACK;`. Read-write mode passes `--single-transaction` instead.
- SQL statements arrive on **stdin**, never via `-c`, so the wrapper can be prepended.
- Row limiting happens on output in `results.py`. **Never** inject `LIMIT` into the user's SQL.
- Config path: `~/.config/postgres-mcp/config.toml`, overridable with `POSTGRES_MCP_CONFIG`. Secrets never live in the config file; `password_env` names an environment variable.
- Tests set `FASTMCP_DECORATOR_MODE=object` (in `tests/conftest.py`, before importing `server`) so `@mcp.tool()` returns `FunctionTool` objects whose `.fn` attribute tests can call. This mirrors the sibling `gemini-mcp` project.

---

## File Structure

| File | Responsibility |
|---|---|
| `pyproject.toml` | Package metadata, deps, `postgres-mcp` entry point |
| `README.md` | Install, config, MCP client registration, read-only role SQL |
| `server.py` | FastMCP tool definitions only; delegates everything |
| `postgres_mcp/__init__.py` | Empty package marker |
| `postgres_mcp/guard.py` | Scrubber, statement splitter, read-only gate. Pure. |
| `postgres_mcp/config.py` | TOML load, validation, read-only precedence |
| `postgres_mcp/psql.py` | Discovery, install guidance, argv/env/stdin, execution |
| `postgres_mcp/results.py` | CSV row counting, truncation, byte ceiling. Pure. |
| `tests/conftest.py` | Shared fixtures; sets `FASTMCP_DECORATOR_MODE` |
| `tests/test_guard.py` | Gate behaviour — the highest-risk module |
| `tests/test_config.py` | Precedence and validation |
| `tests/test_psql_discovery.py` | Discovery and install guidance |
| `tests/test_psql_exec.py` | argv/env/stdin construction |
| `tests/test_results.py` | Truncation and byte ceiling |
| `tests/test_tools.py` | Tool-level behaviour with mocked psql |
| `tests/test_integration.py` | Live Postgres, auto-skipped when absent |

Dependency direction: `guard` and `results` depend on nothing. `config` depends on nothing. `psql` depends on `config` types and `guard`. `server` depends on all four.

---

## Task 1: Scaffolding and the read-only statement gate

`guard.py` is first because it is pure, carries the most security risk, and needs no other module. Project scaffolding folds in here since the tests need a runnable environment.

**Files:**
- Create: `pyproject.toml`, `.python-version`, `.gitignore`
- Create: `postgres_mcp/__init__.py`, `postgres_mcp/guard.py`
- Test: `tests/__init__.py`, `tests/conftest.py`, `tests/test_guard.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `scrub(sql: str) -> str` — same-length, same-line-count copy with comments/literals/dollar-quoted bodies blanked to spaces
  - `split_statements(sql: str) -> list[Statement]`
  - `check(sql: str, *, read_only: bool) -> GuardResult`
  - `@dataclass(frozen=True) Statement(text: str, scrubbed: str)`
  - `@dataclass(frozen=True) GuardResult(allowed: bool, reason: str | None = None, statement: str | None = None)`

- [ ] **Step 1: Create the project skeleton**

`pyproject.toml`:

```toml
[project]
name = "postgres-mcp"
version = "0.1.0"
description = "MCP server exposing PostgreSQL via the psql command-line client"
readme = "README.md"
requires-python = ">=3.11"
dependencies = [
    # Matches the sibling gemini-mcp project's FastMCP major.
    "fastmcp>=3.2.0,<4",
]

[project.scripts]
postgres-mcp = "server:main"

[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",
    "pytest-mock>=3.12.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["postgres_mcp"]

[tool.hatch.build.targets.wheel.force-include]
"server.py" = "server.py"
```

`.python-version`:

```
3.11
```

`.gitignore` (already present on the branch — write exactly this, so the
overwrite is a no-op; `.superpowers/` is the execution scratch directory and
must stay ignored):

```
__pycache__/
*.pyc
.venv/
.pytest_cache/
dist/
.superpowers/
```

`postgres_mcp/__init__.py` and `tests/__init__.py` are both empty files.

`tests/conftest.py`:

```python
"""Shared pytest fixtures for postgres-mcp tests."""
from __future__ import annotations

import os

# Make @mcp.tool() return FunctionTool objects so tests can access .fn
os.environ.setdefault("FASTMCP_DECORATOR_MODE", "object")

import pytest
```

- [ ] **Step 2: Set up the environment**

```bash
cd ~/workspace/postgres-mcp
uv venv
uv pip install -e ".[dev]"
```

- [ ] **Step 3: Write the failing scrubber tests**

`tests/test_guard.py`:

```python
"""Tests for the read-only statement gate."""
from __future__ import annotations

from postgres_mcp import guard


def test_scrub_preserves_length_and_line_count() -> None:
    """Index alignment with the original is what lets us slice the raw SQL."""
    sql = "SELECT 1; -- a comment\nSELECT 2;"
    scrubbed = guard.scrub(sql)

    assert len(scrubbed) == len(sql)
    assert scrubbed.count("\n") == sql.count("\n")


def test_scrub_blanks_line_comment_contents() -> None:
    sql = "SELECT 1 -- DELETE FROM t"
    assert "DELETE" not in guard.scrub(sql)


def test_scrub_blanks_block_comment_contents() -> None:
    sql = "SELECT /* DROP TABLE t */ 1"
    assert "DROP" not in guard.scrub(sql)


def test_scrub_blanks_string_literal_contents() -> None:
    sql = "SELECT 'DELETE FROM t'"
    assert "DELETE" not in guard.scrub(sql)


def test_scrub_handles_doubled_quote_inside_literal() -> None:
    """'' is an escaped quote, not the end of the literal."""
    sql = "SELECT 'it''s DELETE', 1"
    assert "DELETE" not in guard.scrub(sql)


def test_scrub_blanks_dollar_quoted_body() -> None:
    sql = "SELECT $$ INSERT INTO t VALUES (1) $$"
    assert "INSERT" not in guard.scrub(sql)


def test_scrub_blanks_tagged_dollar_quoted_body() -> None:
    sql = "SELECT $body$ UPDATE t SET x = 1 $body$"
    assert "UPDATE" not in guard.scrub(sql)


def test_scrub_leaves_positional_parameters_alone() -> None:
    """$1 is a parameter placeholder, not a dollar-quote opener."""
    sql = "SELECT * FROM t WHERE id = $1"
    assert guard.scrub(sql) == sql
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `uv run pytest tests/test_guard.py -v`
Expected: FAIL — `ModuleNotFoundError` or `AttributeError: module 'postgres_mcp.guard' has no attribute 'scrub'`

- [ ] **Step 5: Implement the scrubber**

`postgres_mcp/guard.py`:

```python
"""Read-only statement gate for SQL headed to psql.

Pure functions over strings: no I/O, no config knowledge. This is layer 2 of
the three-layer read-only defence in the design spec. The BEGIN READ ONLY
wrapper applied in psql.py (layer 3) is what keeps a bug here from becoming a
data-loss bug.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Matches $$ and $tag$ but deliberately not $1, which is a parameter.
_DOLLAR_TAG_RE = re.compile(r"\$(?:[A-Za-z_]\w*)?\$")


@dataclass(frozen=True)
class Statement:
    """One SQL statement, in original and scrubbed form.

    `text` goes to psql and to error messages. `scrubbed` is what keyword
    analysis runs against, so literals and comments cannot trip the gate.
    """

    text: str
    scrubbed: str


@dataclass(frozen=True)
class GuardResult:
    allowed: bool
    reason: str | None = None
    statement: str | None = None


def scrub(sql: str) -> str:
    """Blank out comments, string literals, quoted identifiers and
    dollar-quoted bodies, preserving length and line structure.

    The result is the same length as the input and has newlines in the same
    positions, so an index or line number in the scrubbed text refers to the
    same place in the original.
    """
    out = list(sql)
    n = len(sql)
    i = 0

    def blank(start: int, end: int) -> None:
        for k in range(start, min(end, n)):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        if sql.startswith("--", i):
            end = sql.find("\n", i)
            end = n if end == -1 else end
            blank(i, end)
            i = end
        elif sql.startswith("/*", i):
            # Postgres block comments nest.
            depth, j = 1, i + 2
            while j < n and depth:
                if sql.startswith("/*", j):
                    depth += 1
                    j += 2
                elif sql.startswith("*/", j):
                    depth -= 1
                    j += 2
                else:
                    j += 1
            blank(i, j)
            i = j
        elif sql[i] in "'\"":
            quote = sql[i]
            j = i + 1
            while j < n:
                if sql[j] == quote:
                    if j + 1 < n and sql[j + 1] == quote:
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            blank(i, j)
            i = j
        elif sql[i] == "$":
            match = _DOLLAR_TAG_RE.match(sql, i)
            if match is None:
                i += 1
                continue
            tag = match.group(0)
            end = sql.find(tag, match.end())
            j = n if end == -1 else end + len(tag)
            blank(i, j)
            i = j
        else:
            i += 1

    return "".join(out)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_guard.py -v`
Expected: PASS, 8 tests

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml .python-version .gitignore postgres_mcp/ tests/
git commit -m "feat: add SQL scrubber for the read-only gate"
```

- [ ] **Step 8: Write the failing splitter and gate tests**

Append to `tests/test_guard.py`:

```python
def test_split_ignores_semicolon_inside_literal() -> None:
    statements = guard.split_statements("SELECT 'a;b'")
    assert len(statements) == 1


def test_split_separates_top_level_statements() -> None:
    statements = guard.split_statements("SELECT 1; SELECT 2")
    assert [s.text for s in statements] == ["SELECT 1", "SELECT 2"]


def test_split_drops_empty_trailing_statement() -> None:
    statements = guard.split_statements("SELECT 1;")
    assert [s.text for s in statements] == ["SELECT 1"]


def test_plain_select_allowed_read_only() -> None:
    assert guard.check("SELECT 1", read_only=True).allowed


def test_multi_statement_selects_allowed_read_only() -> None:
    assert guard.check("SELECT 1; SELECT 2;", read_only=True).allowed


def test_cte_select_allowed_read_only() -> None:
    sql = "WITH x AS (SELECT 1 AS n) SELECT n FROM x"
    assert guard.check(sql, read_only=True).allowed


def test_insert_rejected_read_only() -> None:
    result = guard.check("INSERT INTO t VALUES (1)", read_only=True)

    assert not result.allowed
    assert "INSERT" in result.reason


def test_cte_insert_rejected_read_only() -> None:
    """Leads with WITH but writes — the case a leading-keyword check misses."""
    sql = "WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x"
    result = guard.check(sql, read_only=True)

    assert not result.allowed
    assert "INSERT" in result.reason


def test_second_statement_write_rejected_read_only() -> None:
    """Every statement is gated, not just the first."""
    result = guard.check("SELECT 1; DELETE FROM t", read_only=True)

    assert not result.allowed
    assert result.statement == "DELETE FROM t"


def test_explain_analyze_select_allowed_read_only() -> None:
    """A routine read-only diagnostic; rejecting it would be wrong."""
    assert guard.check("EXPLAIN ANALYZE SELECT 1", read_only=True).allowed


def test_explain_with_options_select_allowed_read_only() -> None:
    sql = "EXPLAIN (ANALYZE, BUFFERS) SELECT 1"
    assert guard.check(sql, read_only=True).allowed


def test_explain_analyze_update_rejected_read_only() -> None:
    """EXPLAIN ANALYZE executes what it wraps, so the inner statement is gated."""
    result = guard.check("EXPLAIN ANALYZE UPDATE t SET x = 1", read_only=True)

    assert not result.allowed
    assert "UPDATE" in result.reason


def test_bare_explain_allowed_read_only() -> None:
    assert guard.check("EXPLAIN SELECT 1", read_only=True).allowed


def test_select_for_update_rejected_read_only() -> None:
    result = guard.check("SELECT * FROM t FOR UPDATE", read_only=True)

    assert not result.allowed
    assert "FOR UPDATE" in result.reason or "lock" in result.reason.lower()


def test_select_for_share_rejected_read_only() -> None:
    assert not guard.check("SELECT * FROM t FOR SHARE", read_only=True).allowed


def test_set_rejected_read_only() -> None:
    """No SET means read-only queries must schema-qualify their tables."""
    assert not guard.check("SET search_path = other", read_only=True).allowed


def test_drop_rejected_read_only() -> None:
    assert not guard.check("DROP TABLE t", read_only=True).allowed


def test_keyword_inside_identifier_allowed_read_only() -> None:
    """insert_ts is a column name, not the INSERT keyword."""
    assert guard.check("SELECT insert_ts FROM t", read_only=True).allowed


def test_lowercase_write_rejected_read_only() -> None:
    assert not guard.check("delete from t", read_only=True).allowed


def test_write_allowed_when_not_read_only() -> None:
    assert guard.check("DELETE FROM t", read_only=False).allowed


def test_meta_command_rejected_read_only() -> None:
    result = guard.check("\\! rm -rf /", read_only=True)

    assert not result.allowed
    assert "meta-command" in result.reason


def test_meta_command_rejected_in_read_write_mode() -> None:
    """The ban is unconditional: \\! is command execution, not SQL."""
    assert not guard.check("\\! echo hi", read_only=False).allowed


def test_copy_meta_command_rejected() -> None:
    assert not guard.check("\\copy t TO '/tmp/x.csv'", read_only=False).allowed


def test_indented_meta_command_rejected() -> None:
    assert not guard.check("   \\o /tmp/out", read_only=False).allowed


def test_backslash_inside_literal_not_a_meta_command() -> None:
    """A line starting with \\! inside a string literal is data, not a command."""
    sql = "SELECT '\n\\! echo hi\n'"
    assert guard.check(sql, read_only=False).allowed
```

- [ ] **Step 9: Run the tests to verify they fail**

Run: `uv run pytest tests/test_guard.py -v`
Expected: FAIL — `module 'postgres_mcp.guard' has no attribute 'split_statements'`

- [ ] **Step 10: Implement the splitter and gate**

Append to `postgres_mcp/guard.py`:

```python
_ALLOWED_LEAD = frozenset({"SELECT", "WITH", "EXPLAIN", "SHOW", "TABLE", "VALUES"})

# The four verbs a CTE can reach behind an allowed opener. Everything else
# that writes is already excluded by the leading-keyword allowlist.
_WRITE_RE = re.compile(r"\b(INSERT|UPDATE|DELETE|MERGE)\b", re.IGNORECASE)

# Row-locking reads. Checked before _WRITE_RE so "SELECT ... FOR UPDATE"
# reports the lock rather than a misleading "contains UPDATE".
_LOCK_RE = re.compile(
    r"\bFOR\s+(?:NO\s+KEY\s+)?(?:UPDATE|SHARE|KEY\s+SHARE)\b", re.IGNORECASE
)

_LEAD_RE = re.compile(r"\s*([A-Za-z_]\w*)")

# EXPLAIN, optionally followed by a (...) option list or bare ANALYZE/VERBOSE.
_EXPLAIN_LEAD_RE = re.compile(
    r"\s*EXPLAIN\s*(?:\([^)]*\)|(?:(?:ANALYZE|ANALYSE|VERBOSE)\s+)*)", re.IGNORECASE
)

_META_REASON = (
    "psql meta-commands are never permitted: they can run shell commands "
    "(\\!) and write local files (\\copy, \\o)"
)


def split_statements(sql: str) -> list[Statement]:
    """Split on top-level semicolons, ignoring those inside literals."""
    scrubbed = scrub(sql)
    statements: list[Statement] = []
    start = 0

    for index, char in enumerate(scrubbed):
        if char != ";":
            continue
        if sql[start:index].strip():
            statements.append(
                Statement(sql[start:index].strip(), scrubbed[start:index].strip())
            )
        start = index + 1

    if sql[start:].strip():
        statements.append(Statement(sql[start:].strip(), scrubbed[start:].strip()))

    return statements


def check(sql: str, *, read_only: bool) -> GuardResult:
    """Decide whether `sql` may run against a database in the given mode."""
    meta = _find_meta_command(sql)
    if meta is not None:
        return GuardResult(False, _META_REASON, meta)

    if not read_only:
        return GuardResult(True)

    for statement in split_statements(sql):
        reason = _reject_reason(statement.scrubbed)
        if reason is not None:
            return GuardResult(False, reason, statement.text)

    return GuardResult(True)


def _find_meta_command(sql: str) -> str | None:
    """Return the offending line if any line opens a psql meta-command.

    Scanning the scrubbed copy means a backslash inside a string literal is not
    mistaken for a command; scrub preserves line structure, so line N of the
    scrubbed text is line N of the original.
    """
    original = sql.splitlines()
    for index, line in enumerate(scrub(sql).splitlines()):
        if line.lstrip().startswith("\\"):
            return original[index].strip() if index < len(original) else line.strip()
    return None


def _reject_reason(scrubbed: str) -> str | None:
    """Return why this statement is not read-only, or None if it is."""
    inner = _strip_explain(scrubbed)

    match = _LEAD_RE.match(inner)
    if match is None:
        return "no SQL keyword found at the start of the statement"

    lead = match.group(1).upper()
    if lead not in _ALLOWED_LEAD:
        return (
            f"{lead} is not a read-only statement and this database is "
            "configured read-only"
        )

    lock = _LOCK_RE.search(inner)
    if lock is not None:
        return (
            f"{lock.group(0).upper()} takes row locks, which a read-only "
            "transaction refuses"
        )

    write = _WRITE_RE.search(inner)
    if write is not None:
        return (
            f"statement contains {write.group(1).upper()}, which writes, and "
            "this database is configured read-only"
        )

    return None


def _strip_explain(scrubbed: str) -> str:
    """Return what EXPLAIN wraps, so the gate applies to the inner statement.

    EXPLAIN ANALYZE executes its argument, so `EXPLAIN ANALYZE UPDATE ...`
    must be rejected while `EXPLAIN ANALYZE SELECT ...` is permitted.
    """
    match = _EXPLAIN_LEAD_RE.match(scrubbed)
    if match is None or match.end() == 0:
        return scrubbed
    remainder = scrubbed[match.end() :]
    return remainder if remainder.strip() else scrubbed
```

- [ ] **Step 11: Run the full guard suite**

Run: `uv run pytest tests/test_guard.py -v`
Expected: PASS, 33 tests

- [ ] **Step 12: Commit**

```bash
git add postgres_mcp/guard.py tests/test_guard.py
git commit -m "feat: add read-only statement gate with EXPLAIN recursion"
```

---

## Task 2: Configuration loading and read-only precedence

**Files:**
- Create: `postgres_mcp/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `@dataclass(frozen=True) Database` with fields `name: str`, `read_only: bool`, `statement_timeout: str`, `max_rows: int`, `dsn: str | None`, `host: str | None`, `port: int | None`, `user: str | None`, `dbname: str | None`, `sslmode: str | None`, `password_env: str | None`
  - `@dataclass(frozen=True) Config(databases: dict[str, Database], force_read_only: bool)` with method `get(name: str) -> Database` raising `ConfigError` listing valid names
  - `class ConfigError(Exception)`
  - `config_path(env: Mapping[str, str] | None = None) -> Path`
  - `load_config(path: Path | str | None = None, *, env: Mapping[str, str] | None = None, force_read_only: bool = False) -> Config`

- [ ] **Step 1: Write the failing tests**

`tests/test_config.py`:

```python
"""Tests for config parsing, validation and read-only precedence."""
from __future__ import annotations

from pathlib import Path

import pytest

from postgres_mcp import config as cfg


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body)
    return path


def test_config_path_prefers_env_override() -> None:
    env = {"POSTGRES_MCP_CONFIG": "/tmp/custom.toml"}
    assert cfg.config_path(env) == Path("/tmp/custom.toml")


def test_config_path_defaults_under_dot_config() -> None:
    path = cfg.config_path({})
    assert path.parts[-3:] == (".config", "postgres-mcp", "config.toml")


def test_missing_file_raises_with_path_in_message(tmp_path: Path) -> None:
    missing = tmp_path / "absent.toml"
    with pytest.raises(cfg.ConfigError, match="absent.toml"):
        cfg.load_config(missing, env={})


def test_loads_dsn_entry(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        """
        [databases.local]
        dsn = "postgresql://me@localhost:5432/appdb"
        read_only = false
        """,
    )
    config = cfg.load_config(path, env={})

    assert config.get("local").dsn == "postgresql://me@localhost:5432/appdb"
    assert config.get("local").read_only is False


def test_loads_discrete_field_entry(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        """
        [databases.prod]
        host = "db.example.com"
        port = 5432
        user = "reader"
        dbname = "app"
        sslmode = "require"
        """,
    )
    database = cfg.load_config(path, env={}).get("prod")

    assert database.host == "db.example.com"
    assert database.port == 5432
    assert database.dbname == "app"
    assert database.sslmode == "require"


def test_read_only_defaults_to_true_when_unspecified(tmp_path: Path) -> None:
    """Safe by default: silence means read-only."""
    path = write_config(tmp_path, '[databases.x]\ndbname = "app"\n')
    assert cfg.load_config(path, env={}).get("x").read_only is True


def test_defaults_section_supplies_read_only(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        """
        [defaults]
        read_only = false

        [databases.x]
        dbname = "app"
        """,
    )
    assert cfg.load_config(path, env={}).get("x").read_only is False


def test_per_database_read_only_overrides_defaults(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        """
        [defaults]
        read_only = false

        [databases.x]
        dbname = "app"
        read_only = true
        """,
    )
    assert cfg.load_config(path, env={}).get("x").read_only is True


def test_cli_flag_tightens_read_write_entry(tmp_path: Path) -> None:
    """The global flag can only tighten, never loosen."""
    path = write_config(
        tmp_path, '[databases.x]\ndbname = "app"\nread_only = false\n'
    )
    config = cfg.load_config(path, env={}, force_read_only=True)

    assert config.get("x").read_only is True
    assert config.force_read_only is True


def test_env_flag_tightens_read_write_entry(tmp_path: Path) -> None:
    path = write_config(
        tmp_path, '[databases.x]\ndbname = "app"\nread_only = false\n'
    )
    env = {"POSTGRES_MCP_READ_ONLY": "1"}

    assert cfg.load_config(path, env=env).get("x").read_only is True


def test_dsn_with_discrete_fields_is_an_error(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        """
        [databases.x]
        dsn = "postgresql://localhost/app"
        host = "elsewhere"
        """,
    )
    with pytest.raises(cfg.ConfigError, match="both"):
        cfg.load_config(path, env={})


def test_entry_without_dsn_or_dbname_is_an_error(tmp_path: Path) -> None:
    path = write_config(tmp_path, '[databases.x]\nuser = "me"\n')
    with pytest.raises(cfg.ConfigError, match="dsn"):
        cfg.load_config(path, env={})


def test_no_databases_section_is_an_error(tmp_path: Path) -> None:
    path = write_config(tmp_path, "[defaults]\nread_only = true\n")
    with pytest.raises(cfg.ConfigError, match="no databases"):
        cfg.load_config(path, env={})


def test_password_env_naming_unset_variable_is_an_error(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        '[databases.x]\ndbname = "app"\npassword_env = "MISSING_PW"\n',
    )
    with pytest.raises(cfg.ConfigError, match="MISSING_PW"):
        cfg.load_config(path, env={})


def test_password_env_present_is_accepted(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        '[databases.x]\ndbname = "app"\npassword_env = "PW"\n',
    )
    config = cfg.load_config(path, env={"PW": "secret"})

    assert config.get("x").password_env == "PW"


def test_invalid_statement_timeout_is_an_error(tmp_path: Path) -> None:
    """The value is interpolated into SQL, so its format is validated."""
    path = write_config(
        tmp_path,
        '[databases.x]\ndbname = "app"\nstatement_timeout = "30s; DROP TABLE t"\n',
    )
    with pytest.raises(cfg.ConfigError, match="statement_timeout"):
        cfg.load_config(path, env={})


def test_valid_statement_timeout_units_accepted(tmp_path: Path) -> None:
    for value in ("30s", "500ms", "2min", "1h", "45"):
        assert cfg._validate_timeout(value, "x") == value


def test_non_positive_max_rows_is_an_error(tmp_path: Path) -> None:
    path = write_config(tmp_path, '[databases.x]\ndbname = "app"\nmax_rows = 0\n')
    with pytest.raises(cfg.ConfigError, match="max_rows"):
        cfg.load_config(path, env={})


def test_get_unknown_database_lists_valid_names(tmp_path: Path) -> None:
    path = write_config(
        tmp_path, '[databases.alpha]\ndbname = "a"\n\n[databases.beta]\ndbname = "b"\n'
    )
    config = cfg.load_config(path, env={})

    with pytest.raises(cfg.ConfigError, match="alpha, beta"):
        config.get("gamma")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'postgres_mcp.config'`

- [ ] **Step 3: Implement the config module**

`postgres_mcp/config.py`:

```python
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

    forced = force_read_only or environ.get(
        "POSTGRES_MCP_READ_ONLY", ""
    ).strip().lower() in _TRUTHY

    defaults = raw.get("defaults", {})
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
    read_only = True if forced else bool(declared)

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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS, 19 tests

- [ ] **Step 5: Commit**

```bash
git add postgres_mcp/config.py tests/test_config.py
git commit -m "feat: add config loading with read-only precedence"
```

---

## Task 3: psql discovery and install guidance

**Files:**
- Create: `postgres_mcp/psql.py`
- Test: `tests/test_psql_discovery.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `class PsqlNotFound(Exception)` with attribute `guidance: str`
  - `find_psql(env: Mapping[str, str] | None = None, *, platform_name: str | None = None) -> str`
  - `install_guidance(platform_name: str, found_at: str | None = None) -> str`
  - `PROBE_PATHS: tuple[str, ...]`

- [ ] **Step 1: Write the failing tests**

`tests/test_psql_discovery.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_psql_discovery.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'postgres_mcp.psql'`

- [ ] **Step 3: Implement discovery**

`postgres_mcp/psql.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_psql_discovery.py -v`
Expected: PASS, 7 tests

- [ ] **Step 5: Commit**

```bash
git add postgres_mcp/psql.py tests/test_psql_discovery.py
git commit -m "feat: locate psql with probe fallback and install guidance"
```

---

## Task 4: psql invocation

**Files:**
- Modify: `postgres_mcp/psql.py` (append)
- Test: `tests/test_psql_exec.py`

**Interfaces:**
- Consumes: `config.Database` (Task 2); `guard.check` (Task 1); `find_psql`, `PsqlNotFound` (Task 3).
- Produces:
  - `@dataclass(frozen=True) PsqlResult(ok: bool, stdout: str, stderr: str, returncode: int)`
  - `build_argv(psql_path: str, db: Database, *, read_only: bool, variables: Mapping[str, str] | None = None) -> list[str]`
  - `build_env(db: Database, base_env: Mapping[str, str]) -> dict[str, str]`
  - `build_input(sql: str, db: Database, *, read_only: bool) -> str`
  - `run_sql(db: Database, sql: str, *, read_only: bool, variables: Mapping[str, str] | None = None, env: Mapping[str, str] | None = None, psql_path: str | None = None, timeout: float = 60.0) -> PsqlResult`
  - `class GuardRejected(Exception)` with attribute `result: guard.GuardResult`

- [ ] **Step 1: Write the failing tests**

`tests/test_psql_exec.py`:

```python
"""Tests for psql argv, environment and stdin construction."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from postgres_mcp import psql
from postgres_mcp.config import Database


def make_db(**overrides: object) -> Database:
    defaults: dict[str, object] = {
        "name": "test",
        "read_only": True,
        "statement_timeout": "30s",
        "max_rows": 1000,
        "dbname": "app",
    }
    defaults.update(overrides)
    return Database(**defaults)  # type: ignore[arg-type]


def test_argv_always_includes_the_mandatory_flags() -> None:
    argv = psql.build_argv("/bin/psql", make_db(), read_only=True)

    assert "--no-psqlrc" in argv
    assert "--csv" in argv
    assert "ON_ERROR_STOP=1" in argv


def test_argv_uses_dsn_as_positional_when_present() -> None:
    db = make_db(dsn="postgresql://me@localhost/app", dbname=None)
    argv = psql.build_argv("/bin/psql", db, read_only=True)

    assert argv[-1] == "postgresql://me@localhost/app"


def test_argv_uses_discrete_flags_when_no_dsn() -> None:
    db = make_db(host="db.example.com", port=5433, user="reader", dbname="app")
    argv = psql.build_argv("/bin/psql", db, read_only=True)

    assert "-h" in argv and "db.example.com" in argv
    assert "-p" in argv and "5433" in argv
    assert "-U" in argv and "reader" in argv
    assert "-d" in argv and "app" in argv


def test_argv_adds_single_transaction_only_in_read_write_mode() -> None:
    read_write = psql.build_argv("/bin/psql", make_db(), read_only=False)
    read_only = psql.build_argv("/bin/psql", make_db(), read_only=True)

    assert "--single-transaction" in read_write
    assert "--single-transaction" not in read_only


def test_argv_passes_psql_variables() -> None:
    argv = psql.build_argv(
        "/bin/psql", make_db(), read_only=True, variables={"schema": "public"}
    )

    assert "schema=public" in argv


def test_password_never_appears_in_argv() -> None:
    db = make_db(password_env="PW")
    argv = psql.build_argv("/bin/psql", db, read_only=True)

    assert not any("secret" in part for part in argv)


def test_password_is_passed_through_the_environment() -> None:
    db = make_db(password_env="PW")
    env = psql.build_env(db, {"PW": "secret", "PATH": "/bin"})

    assert env["PGPASSWORD"] == "secret"


def test_env_sets_sslmode_and_connect_timeout() -> None:
    env = psql.build_env(make_db(sslmode="require"), {})

    assert env["PGSSLMODE"] == "require"
    assert "PGCONNECT_TIMEOUT" in env


def test_env_omits_pgpassword_without_password_env() -> None:
    assert "PGPASSWORD" not in psql.build_env(make_db(), {})


def test_input_wraps_read_only_statements_in_a_read_only_transaction() -> None:
    text = psql.build_input("SELECT 1", make_db(), read_only=True)

    assert "BEGIN READ ONLY;" in text
    assert "ROLLBACK;" in text
    assert "SELECT 1" in text


def test_input_sets_statement_timeout_in_read_only_mode() -> None:
    text = psql.build_input("SELECT 1", make_db(), read_only=True)
    assert "SET statement_timeout = '30s';" in text


def test_input_sets_statement_timeout_in_read_write_mode_too() -> None:
    """A runaway statement must not hang the call in either mode."""
    text = psql.build_input("DELETE FROM t", make_db(), read_only=False)

    assert "SET statement_timeout = '30s';" in text
    assert "BEGIN READ ONLY;" not in text


def test_input_terminates_an_unterminated_statement() -> None:
    text = psql.build_input("SELECT 1", make_db(), read_only=True)
    assert "SELECT 1;" in text


def test_run_sql_rejects_write_in_read_only_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate runs before any process is spawned."""
    spawned = MagicMock()
    monkeypatch.setattr(psql.subprocess, "run", spawned)
    monkeypatch.setattr(psql, "find_psql", lambda *a, **k: "/bin/psql")

    with pytest.raises(psql.GuardRejected):
        psql.run_sql(make_db(), "DELETE FROM t", read_only=True)

    spawned.assert_not_called()


def test_run_sql_returns_stdout_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    completed = MagicMock(returncode=0, stdout="n\n1\n", stderr="")
    monkeypatch.setattr(psql.subprocess, "run", lambda *a, **k: completed)
    monkeypatch.setattr(psql, "find_psql", lambda *a, **k: "/bin/psql")

    result = psql.run_sql(make_db(), "SELECT 1", read_only=True)

    assert result.ok is True
    assert result.stdout == "n\n1\n"


def test_run_sql_reports_failure_with_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    completed = MagicMock(returncode=1, stdout="", stderr='ERROR: relation "t" does not exist')
    monkeypatch.setattr(psql.subprocess, "run", lambda *a, **k: completed)
    monkeypatch.setattr(psql, "find_psql", lambda *a, **k: "/bin/psql")

    result = psql.run_sql(make_db(), "SELECT * FROM t", read_only=True)

    assert result.ok is False
    assert "does not exist" in result.stderr


def test_run_sql_sends_sql_on_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        captured["argv"] = argv
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(psql.subprocess, "run", fake_run)
    monkeypatch.setattr(psql, "find_psql", lambda *a, **k: "/bin/psql")

    psql.run_sql(make_db(), "SELECT 1", read_only=True)

    assert "SELECT 1" in captured["input"]
    assert "-c" not in captured["argv"]


def test_run_sql_reports_a_timeout_as_a_failed_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TimeoutExpired is not an OSError, so it must not escape to the caller."""

    def raise_timeout(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise psql.subprocess.TimeoutExpired(cmd="psql", timeout=60.0)

    monkeypatch.setattr(psql.subprocess, "run", raise_timeout)
    monkeypatch.setattr(psql, "find_psql", lambda *a, **k: "/bin/psql")

    result = psql.run_sql(make_db(), "SELECT pg_sleep(99)", read_only=True)

    assert result.ok is False
    assert "did not finish" in result.stderr
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_psql_exec.py -v`
Expected: FAIL — `module 'postgres_mcp.psql' has no attribute 'build_argv'`

- [ ] **Step 3: Implement invocation**

Add these imports at the top of `postgres_mcp/psql.py`:

```python
import subprocess
from dataclasses import dataclass

from postgres_mcp import guard
from postgres_mcp.config import Database
```

Append to `postgres_mcp/psql.py`:

```python
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
    if not body.endswith(";"):
        body += ";"

    lines = [f"SET statement_timeout = '{db.statement_timeout}';"]
    if read_only:
        lines += ["BEGIN READ ONLY;", body, "ROLLBACK;"]
    else:
        lines.append(body)

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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_psql_exec.py -v`
Expected: PASS, 18 tests

- [ ] **Step 5: Run the whole suite to check nothing regressed**

Run: `uv run pytest -v`
Expected: PASS, 77 tests

- [ ] **Step 6: Commit**

```bash
git add postgres_mcp/psql.py tests/test_psql_exec.py
git commit -m "feat: build and run psql invocations with read-only wrapper"
```

---

## Task 5: CSV result shaping

**Files:**
- Create: `postgres_mcp/results.py`
- Modify: `docs/superpowers/specs/2026-09-08-postgres-mcp-design.md` (correct one open-risk sentence)
- Test: `tests/test_results.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `@dataclass(frozen=True) Rendered(text: str, row_count: int, truncated: bool)`
  - `render_csv(csv_text: str, *, max_rows: int, max_bytes: int = MAX_BYTES) -> Rendered`
  - `MAX_BYTES: int`

- [ ] **Step 1: Write the failing tests**

`tests/test_results.py`:

```python
"""Tests for CSV truncation and the byte ceiling."""
from __future__ import annotations

from postgres_mcp import results


def test_small_result_passes_through_untruncated() -> None:
    rendered = results.render_csv("n\n1\n2\n", max_rows=10)

    assert rendered.row_count == 2
    assert rendered.truncated is False
    assert "truncated" not in rendered.text


def test_empty_result_reports_zero_rows() -> None:
    rendered = results.render_csv("n\n", max_rows=10)

    assert rendered.row_count == 0
    assert rendered.truncated is False


def test_blank_output_is_handled() -> None:
    rendered = results.render_csv("", max_rows=10)

    assert rendered.row_count == 0
    assert rendered.text == ""


def test_row_limit_truncates_and_reports_the_real_total() -> None:
    csv_text = "n\n" + "".join(f"{i}\n" for i in range(50))
    rendered = results.render_csv(csv_text, max_rows=10)

    assert rendered.row_count == 50
    assert rendered.truncated is True
    assert "-- truncated: showing 10 of 50 rows" in rendered.text


def test_header_survives_truncation() -> None:
    csv_text = "id,name\n" + "".join(f"{i},x\n" for i in range(20))
    rendered = results.render_csv(csv_text, max_rows=5)

    assert rendered.text.splitlines()[0] == "id,name"


def test_embedded_newline_in_quoted_field_counts_as_one_row() -> None:
    """A naive splitlines() count would report two rows here."""
    rendered = results.render_csv('n\n"line one\nline two"\n', max_rows=10)

    assert rendered.row_count == 1


def test_byte_ceiling_trims_further_than_the_row_limit() -> None:
    wide = "x" * 500
    csv_text = "col\n" + "".join(f"{wide}\n" for _ in range(100))
    rendered = results.render_csv(csv_text, max_rows=100, max_bytes=2000)

    assert rendered.truncated is True
    assert len(rendered.text.encode()) <= 2000 + 200  # trailer allowance


def test_byte_ceiling_keeps_at_least_the_header() -> None:
    csv_text = "col\n" + "y" * 5000 + "\n"
    rendered = results.render_csv(csv_text, max_rows=10, max_bytes=100)

    assert rendered.text.splitlines()[0] == "col"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_results.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'postgres_mcp.results'`

- [ ] **Step 3: Implement result shaping**

`postgres_mcp/results.py`:

```python
"""Shape psql CSV output for a model's context window.

Rows are counted with the csv module rather than by splitting on newlines: a
quoted field may contain newlines, and a naive count would both mis-report the
total and cut a row in half.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass

MAX_BYTES = 100_000


@dataclass(frozen=True)
class Rendered:
    text: str
    row_count: int
    truncated: bool


def render_csv(
    csv_text: str, *, max_rows: int, max_bytes: int = MAX_BYTES
) -> Rendered:
    """Return display text, the true row count, and whether it was cut."""
    if not csv_text.strip():
        return Rendered(text="", row_count=0, truncated=False)

    rows = list(csv.reader(io.StringIO(csv_text)))
    header, data = rows[0], [row for row in rows[1:] if row]
    total = len(data)

    kept = data[:max_rows]
    truncated = len(kept) < total

    text = _serialise(header, kept)
    while len(text.encode()) > max_bytes and kept:
        # Drop a proportional chunk rather than one row at a time.
        kept = kept[: max(1, len(kept) // 2)] if len(kept) > 1 else []
        truncated = True
        text = _serialise(header, kept)

    if truncated:
        text += f"\n-- truncated: showing {len(kept)} of {total} rows"

    return Rendered(text=text, row_count=total, truncated=truncated)


def _serialise(header: list[str], rows: list[list[str]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().rstrip("\n")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_results.py -v`
Expected: PASS, 8 tests

- [ ] **Step 5: Correct the spec's open-risk note**

The spec's "Open risks" section says output is "passed through as text rather than parsed". `render_csv` parses it, deliberately, to count rows correctly. Replace that bullet in `docs/superpowers/specs/2026-09-08-postgres-mcp-design.md` with:

```markdown
- `results.py` parses psql's CSV with Python's `csv` module rather than passing
  it through as opaque text. Correct row counting requires it: a quoted field
  may contain newlines, so splitting on newlines both mis-reports the total and
  can cut a row in half. Python's `csv` dialect and psql's `--csv` output are
  both RFC 4180, so the exposure is a quoting divergence between them rather
  than a parsing bug of our own.
```

- [ ] **Step 6: Commit**

```bash
git add postgres_mcp/results.py tests/test_results.py docs/superpowers/specs/
git commit -m "feat: add CSV truncation with row-accurate counting"
```

---

## Task 6: `list_databases` and `execute_sql`

**Files:**
- Create: `server.py`
- Test: `tests/test_tools.py`
- Modify: `tests/conftest.py` (add shared fixtures)

**Interfaces:**
- Consumes: `config.load_config`, `config.Config`, `config.ConfigError`, `config.Database`; `psql.run_sql`, `psql.PsqlResult`, `psql.GuardRejected`, `psql.PsqlNotFound`; `results.render_csv`.
- Produces:
  - `mcp: FastMCP` named `"postgres"`
  - `_get_config() -> Config` (cached; `_config` and `_force_read_only` module globals)
  - `list_databases()` tool returning `dict[str, object]`
  - `execute_sql(database: str, query: str, max_rows: int | None = None)` tool returning `str`
  - `main() -> None` parsing `--read-only`

- [ ] **Step 1: Add fixtures to `tests/conftest.py`**

Append to `tests/conftest.py`:

```python
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
```

- [ ] **Step 2: Write the failing tests**

`tests/test_tools.py`:

```python
"""Tool-level tests with psql mocked out."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from postgres_mcp import psql as psql_mod


def test_list_databases_reports_each_entry(fresh_server) -> None:  # type: ignore[no-untyped-def]
    listing = fresh_server.list_databases.fn()
    names = {entry["name"] for entry in listing["databases"]}

    assert names == {"ro", "rw", "withpw"}


def test_list_databases_reports_effective_read_only(fresh_server) -> None:  # type: ignore[no-untyped-def]
    by_name = {e["name"]: e for e in fresh_server.list_databases.fn()["databases"]}

    assert by_name["ro"]["read_only"] is True
    assert by_name["rw"]["read_only"] is False


def test_list_databases_never_leaks_secrets(fresh_server) -> None:  # type: ignore[no-untyped-def]
    """The withpw entry has a real password in the environment; none of it,
    nor the variable name holding it, may appear in the listing."""
    rendered = repr(fresh_server.list_databases.fn())

    assert "super-secret-value" not in rendered
    assert "PGPASSWORD" not in rendered
    assert "password_env" not in rendered


def test_execute_sql_returns_rendered_rows(
    fresh_server, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        fresh_server.psql,
        "run_sql",
        lambda *a, **k: psql_mod.PsqlResult(True, "n\n1\n2\n", "", 0),
    )
    output = fresh_server.execute_sql.fn("ro", "SELECT 1")

    assert "n" in output
    assert "1" in output


def test_execute_sql_passes_read_only_from_config(
    fresh_server, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    """The mode comes from config, never from the caller."""
    captured: dict[str, object] = {}

    def fake_run(db, sql, **kwargs):  # type: ignore[no-untyped-def]
        captured["read_only"] = kwargs["read_only"]
        return psql_mod.PsqlResult(True, "n\n", "", 0)

    monkeypatch.setattr(fresh_server.psql, "run_sql", fake_run)

    fresh_server.execute_sql.fn("rw", "SELECT 1")
    assert captured["read_only"] is False

    fresh_server.execute_sql.fn("ro", "SELECT 1")
    assert captured["read_only"] is True


def test_execute_sql_has_no_read_only_parameter(fresh_server) -> None:  # type: ignore[no-untyped-def]
    """The model must not be able to escalate its own privileges."""
    import inspect

    signature = inspect.signature(fresh_server.execute_sql.fn)
    assert "read_only" not in signature.parameters


def test_execute_sql_unknown_database_lists_valid_names(fresh_server) -> None:  # type: ignore[no-untyped-def]
    output = fresh_server.execute_sql.fn("nope", "SELECT 1")

    assert "ro" in output and "rw" in output


def test_execute_sql_surfaces_guard_rejection(
    fresh_server, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    from postgres_mcp import guard

    rejection = psql_mod.GuardRejected(
        guard.GuardResult(False, "contains DELETE", "DELETE FROM t")
    )
    monkeypatch.setattr(
        fresh_server.psql, "run_sql", MagicMock(side_effect=rejection)
    )
    output = fresh_server.execute_sql.fn("ro", "DELETE FROM t")

    assert "contains DELETE" in output


def test_execute_sql_surfaces_psql_errors(
    fresh_server, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        fresh_server.psql,
        "run_sql",
        lambda *a, **k: psql_mod.PsqlResult(False, "", 'ERROR: no relation "t"', 1),
    )
    output = fresh_server.execute_sql.fn("ro", "SELECT * FROM t")

    assert "no relation" in output


def test_execute_sql_surfaces_install_guidance(
    fresh_server, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    missing = psql_mod.PsqlNotFound("psql was not found. brew install libpq")
    monkeypatch.setattr(
        fresh_server.psql, "run_sql", MagicMock(side_effect=missing)
    )
    output = fresh_server.execute_sql.fn("ro", "SELECT 1")

    assert "brew install libpq" in output


def test_execute_sql_honours_max_rows_override(
    fresh_server, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    csv_text = "n\n" + "".join(f"{i}\n" for i in range(30))
    monkeypatch.setattr(
        fresh_server.psql,
        "run_sql",
        lambda *a, **k: psql_mod.PsqlResult(True, csv_text, "", 0),
    )
    output = fresh_server.execute_sql.fn("ro", "SELECT 1", max_rows=5)

    assert "showing 5 of 30 rows" in output


def test_force_read_only_tightens_a_writable_database(
    fresh_server, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    fresh_server._force_read_only = True
    fresh_server._config = None
    captured: dict[str, object] = {}

    def fake_run(db, sql, **kwargs):  # type: ignore[no-untyped-def]
        captured["read_only"] = kwargs["read_only"]
        return psql_mod.PsqlResult(True, "n\n", "", 0)

    monkeypatch.setattr(fresh_server.psql, "run_sql", fake_run)
    fresh_server.execute_sql.fn("rw", "SELECT 1")

    assert captured["read_only"] is True
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_tools.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'server'`

- [ ] **Step 4: Implement the server and two tools**

`server.py`:

```python
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
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_tools.py -v`
Expected: PASS, 12 tests

- [ ] **Step 6: Commit**

```bash
git add server.py tests/test_tools.py tests/conftest.py
git commit -m "feat: add list_databases and execute_sql tools"
```

---

## Task 7: `describe_schema` and `test_connection`

**Files:**
- Modify: `server.py` (append two tools)
- Test: `tests/test_tools.py` (append)

**Interfaces:**
- Consumes: everything from Task 6, plus `psql.run_sql`'s `variables` parameter (Task 4) and `psql.find_psql` (Task 3).
- Produces:
  - `describe_schema(database: str, schema: str = "public", table: str | None = None) -> str` tool
  - `test_connection(database: str | None = None) -> str` tool

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tools.py`:

```python
def test_describe_schema_lists_tables(
    fresh_server, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        fresh_server.psql,
        "run_sql",
        lambda *a, **k: psql_mod.PsqlResult(True, "table,columns\nusers,4\n", "", 0),
    )
    output = fresh_server.describe_schema.fn("ro")

    assert "users" in output


def test_describe_schema_passes_schema_as_a_psql_variable(
    fresh_server, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    """Identifiers travel as psql variables, never string-formatted into SQL."""
    captured: dict[str, object] = {}

    def fake_run(db, sql, **kwargs):  # type: ignore[no-untyped-def]
        captured["variables"] = kwargs.get("variables")
        captured["sql"] = sql
        return psql_mod.PsqlResult(True, "table,columns\n", "", 0)

    monkeypatch.setattr(fresh_server.psql, "run_sql", fake_run)
    fresh_server.describe_schema.fn("ro", schema="analytics")

    assert captured["variables"] == {"schema": "analytics"}
    assert "analytics" not in captured["sql"]


def test_describe_schema_always_runs_read_only(
    fresh_server, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    """Even against a writable database, introspection only reads."""
    captured: dict[str, object] = {}

    def fake_run(db, sql, **kwargs):  # type: ignore[no-untyped-def]
        captured["read_only"] = kwargs["read_only"]
        return psql_mod.PsqlResult(True, "table,columns\n", "", 0)

    monkeypatch.setattr(fresh_server.psql, "run_sql", fake_run)
    fresh_server.describe_schema.fn("rw")

    assert captured["read_only"] is True


def test_describe_schema_with_table_returns_labelled_sections(
    fresh_server, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        fresh_server.psql,
        "run_sql",
        lambda *a, **k: psql_mod.PsqlResult(True, "a,b\n1,2\n", "", 0),
    )
    output = fresh_server.describe_schema.fn("ro", table="users")

    assert "Columns" in output
    assert "Constraints" in output
    assert "Indexes" in output


def test_describe_schema_rejects_invalid_identifier(fresh_server) -> None:  # type: ignore[no-untyped-def]
    output = fresh_server.describe_schema.fn("ro", table="users; DROP TABLE x")

    assert "invalid" in output.lower()


def test_test_connection_reports_psql_and_server(
    fresh_server, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(fresh_server.psql, "find_psql", lambda *a, **k: "/bin/psql")
    monkeypatch.setattr(
        fresh_server.psql,
        "run_sql",
        lambda *a, **k: psql_mod.PsqlResult(
            True, "version,current_user\nPostgreSQL 18.6,reader\n", "", 0
        ),
    )
    output = fresh_server.test_connection.fn("ro")

    assert "/bin/psql" in output
    assert "PostgreSQL 18.6" in output


def test_test_connection_checks_every_database_when_unspecified(
    fresh_server, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(fresh_server.psql, "find_psql", lambda *a, **k: "/bin/psql")
    monkeypatch.setattr(
        fresh_server.psql,
        "run_sql",
        lambda *a, **k: psql_mod.PsqlResult(True, "version\nPostgreSQL 18.6\n", "", 0),
    )
    output = fresh_server.test_connection.fn()

    assert "ro" in output and "rw" in output


def test_test_connection_reports_install_guidance_when_psql_missing(
    fresh_server, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    def raise_missing(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise psql_mod.PsqlNotFound("psql was not found. brew install libpq")

    monkeypatch.setattr(fresh_server.psql, "find_psql", raise_missing)
    output = fresh_server.test_connection.fn("ro")

    assert "brew install libpq" in output


def test_test_connection_reports_failure_per_database(
    fresh_server, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(fresh_server.psql, "find_psql", lambda *a, **k: "/bin/psql")
    monkeypatch.setattr(
        fresh_server.psql,
        "run_sql",
        lambda *a, **k: psql_mod.PsqlResult(
            False, "", "could not connect to server", 2
        ),
    )
    output = fresh_server.test_connection.fn("ro")

    assert "could not connect" in output
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_tools.py -v`
Expected: FAIL — `module 'server' has no attribute 'describe_schema'`

- [ ] **Step 3: Implement the two tools**

Add to `server.py`'s imports:

```python
import re
```

Insert before `def main()` in `server.py`:

```python
# Identifiers reach psql as variables, but they are still validated so a
# malformed name fails with a clear message instead of a catalog error.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

_TABLE_LIST_SQL = """
-- Aliases that are reserved words are double-quoted: bare `AS table`,
-- `AS column`, `AS default` and `AS type` are syntax errors in PostgreSQL.
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
       current_user AS username,
       current_setting('transaction_read_only') AS read_only
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
    user, and whether the session is read-only. Omit `database` to check every
    configured entry, which also validates the config file.
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_tools.py -v`
Expected: PASS, 21 tests

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest -v`
Expected: PASS, 106 tests (integration tests skip without a local Postgres)

- [ ] **Step 6: Commit**

```bash
git add server.py tests/test_tools.py
git commit -m "feat: add describe_schema and test_connection tools"
```

---

## Task 8: README and live integration test

**Files:**
- Create: `README.md`
- Test: `tests/test_integration.py`

**Interfaces:**
- Consumes: everything.
- Produces: no new code interfaces.

- [ ] **Step 1: Write the integration test**

`tests/test_integration.py`:

```python
"""Integration tests against a real Postgres.

Skipped automatically when psql is missing or nothing answers on localhost.
"""
from __future__ import annotations

import socket

import pytest

from postgres_mcp import psql
from postgres_mcp.config import Database


def _postgres_reachable() -> bool:
    try:
        psql.find_psql()
    except psql.PsqlNotFound:
        return False
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("localhost", 5432)) == 0


pytestmark = pytest.mark.skipif(
    not _postgres_reachable(), reason="no local Postgres on localhost:5432"
)


def make_db(read_only: bool) -> Database:
    return Database(
        name="local",
        read_only=read_only,
        statement_timeout="10s",
        max_rows=100,
        dbname="postgres",
    )


def test_select_returns_rows_from_a_live_server() -> None:
    result = psql.run_sql(make_db(True), "SELECT 1 AS n", read_only=True)

    assert result.ok, result.stderr
    assert "1" in result.stdout


def test_read_only_transaction_blocks_a_write_at_the_server() -> None:
    """Layer 3 in isolation: bypass the gate, let Postgres refuse the write."""
    db = make_db(True)
    text = psql.build_input("CREATE TABLE mcp_probe (id int)", db, read_only=True)
    argv = psql.build_argv(psql.find_psql(), db, read_only=True)

    completed = psql.subprocess.run(
        argv,
        input=text,
        env=psql.build_env(db, dict(psql.os.environ)),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "read-only transaction" in completed.stderr.lower()


def test_statement_timeout_is_applied() -> None:
    db = Database(
        name="local",
        read_only=True,
        statement_timeout="100ms",
        max_rows=10,
        dbname="postgres",
    )
    result = psql.run_sql(db, "SELECT pg_sleep(3)", read_only=True)

    assert result.ok is False
    assert "statement timeout" in result.stderr.lower()


@pytest.mark.parametrize(
    "name",
    ["_TABLE_LIST_SQL", "_COLUMNS_SQL", "_CONSTRAINTS_SQL", "_INDEXES_SQL"],
)
def test_introspection_sql_is_valid_against_a_live_server(name: str) -> None:
    """Execute the catalog queries for real.

    The tool-level tests mock psql away, so a SQL syntax error in these
    queries would otherwise reach a user before a test. Reserved words used as
    column aliases (table, column, default, type) are the likely offender and
    fail only when a server parses them.
    """
    import server

    sql = getattr(server, name)
    result = psql.run_sql(
        make_db(True),
        sql,
        read_only=True,
        variables={"schema": "pg_catalog", "table": "pg_class"},
    )

    assert result.ok, f"{name} failed: {result.stderr}"


def test_connection_sql_is_valid_against_a_live_server() -> None:
    import server

    result = psql.run_sql(make_db(True), server._CONNECTION_SQL, read_only=True)

    assert result.ok, result.stderr
    assert "PostgreSQL" in result.stdout
```

- [ ] **Step 2: Run the integration test**

Run: `uv run pytest tests/test_integration.py -v`
Expected: PASS if a local Postgres is running; SKIPPED otherwise. Both outcomes are acceptable.

- [ ] **Step 3: Write the README**

`README.md`:

````markdown
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
   `UPDATE`, `DELETE` or `MERGE`. `WITH x AS (...) INSERT ...` is caught.
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
````

- [ ] **Step 4: Verify the README's own instructions work**

```bash
cd ~/workspace/postgres-mcp
uv run pytest -v
uv run postgres-mcp --help
```

Expected: the suite passes, and `--help` shows the `--read-only` flag.

- [ ] **Step 5: Commit**

```bash
git add README.md tests/test_integration.py
git commit -m "docs: add README and live Postgres integration test"
```

---

## Verification

After Task 8, confirm end to end:

- [ ] `uv run pytest -v` — full suite green
- [ ] `uv run postgres-mcp --help` — entry point works
- [ ] Write a real config for a live database, then call `test_connection` with no argument and confirm every entry reports its version and mode
- [ ] Against a `read_only = true` entry, confirm `execute_sql` runs a `SELECT` and refuses `DELETE FROM <table>` with a gate message
- [ ] Against that same entry, confirm `execute_sql` refuses `WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x`
- [ ] Confirm `execute_sql` refuses `\! echo hi`
- [ ] Register the server in your MCP client with `--read-only` and confirm the tools appear and `list_databases` reports `force_read_only: true`
