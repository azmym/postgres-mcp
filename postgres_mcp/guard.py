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
