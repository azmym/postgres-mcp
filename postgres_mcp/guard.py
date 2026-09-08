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
