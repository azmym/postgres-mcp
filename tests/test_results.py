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


def test_null_and_empty_string_are_not_distinguished() -> None:
    """Known, accepted limitation: the csv round-trip collapses NULL and ''.

    psql writes NULL as an unquoted empty field and a zero-length string as a
    quoted one; csv.reader reports both as '', so the rendered output cannot
    tell them apart. See the spec's open-risks entry.
    """
    rendered = results.render_csv('a,b\n,""\n', max_rows=10)

    assert rendered.text == "a,b\n,"
