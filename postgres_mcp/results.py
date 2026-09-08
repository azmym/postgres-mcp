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
