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
            # Postgres allows $ inside an identifier after the first character,
            # and its longest-match lexer reads a$b$c as one identifier rather
            # than an identifier followed by a dollar quote. Reading it as a
            # quote here would blank everything to the closing tag -- to end of
            # input when there is none -- hiding statement separators and write
            # keywords from the gate. Erring toward not blanking is the safe
            # direction: the gate then sees more text, never less.
            if i and (sql[i - 1].isalnum() or sql[i - 1] in "_$"):
                i += 1
                continue
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

# The writes an allowed opener can hide: the four DML verbs reachable through a
# CTE, plus INTO, which turns a SELECT into a table-creating statement.
# Everything else that writes is already excluded by the leading-keyword
# allowlist.
_WRITE_RE = re.compile(r"\b(INSERT|UPDATE|DELETE|MERGE|INTO)\b", re.IGNORECASE)

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
    return _split(sql, scrub(sql))


def _split(sql: str, scrubbed: str) -> list[Statement]:
    """Split `sql` using semicolons located in its already-scrubbed copy.

    Takes the scrubbed text as an argument so `check` can scrub once and have
    the meta-command scan and the statement gate reason about identical text.

    A fragment whose scrubbed form is empty after stripping carries no SQL —
    only comments or whitespace — and is dropped, so a trailing comment after
    the final semicolon is not gated as an unparseable statement.
    """
    statements: list[Statement] = []
    start = 0

    for index, char in enumerate(scrubbed):
        if char != ";":
            continue
        if scrubbed[start:index].strip():
            statements.append(
                Statement(sql[start:index].strip(), scrubbed[start:index].strip())
            )
        start = index + 1

    if scrubbed[start:].strip():
        statements.append(Statement(sql[start:].strip(), scrubbed[start:].strip()))

    return statements


def check(sql: str, *, read_only: bool) -> GuardResult:
    """Decide whether `sql` may run against a database in the given mode."""
    scrubbed = scrub(sql)
    statements = _split(sql, scrubbed)

    # Unconditional: \! runs a shell command on this host, which no server-side
    # setting can constrain, so the ban precedes the read_only shortcut below.
    meta = _find_meta_command(sql, scrubbed, statements)
    if meta is not None:
        return GuardResult(False, _META_REASON, meta)

    if not read_only:
        return GuardResult(True)

    for statement in statements:
        reason = _reject_reason(statement.scrubbed)
        if reason is not None:
            return GuardResult(False, reason, statement.text)

    return GuardResult(True)


def _find_meta_command(
    sql: str, scrubbed: str, statements: list[Statement]
) -> str | None:
    """Return the offending text if `sql` opens a psql meta-command anywhere.

    Two scans, because a backslash command reaches psql from two positions and
    neither scan alone sees both:

    - after a statement separator (`SELECT 1; \\! rm -rf /`), where it shares a
      line with SQL and so never starts a line;
    - on its own line inside a statement that has no terminating semicolon
      (`SELECT 1\\n\\! rm -rf /`), where it is not a statement of its own.

    Both read the scrubbed copy, so a backslash inside a string literal stays
    data. Lines are split on "\\n" alone -- the only line break scrub preserves,
    since a \\r or \\f inside a literal is blanked to a space -- which keeps the
    scrubbed and original line lists the same length and index-aligned.
    """
    for statement in statements:
        if statement.text.startswith("\\"):
            return statement.text

    original = sql.split("\n")
    for index, line in enumerate(scrubbed.split("\n")):
        if line.lstrip().startswith("\\"):
            return original[index].strip()
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
