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


def test_render_csv_treats_two_identical_empty_fields_identically() -> None:
    """render_csv cannot tell an unquoted empty field from a quoted one.

    The input is a row whose two fields are an unquoted empty and a quoted
    empty (`""`); csv.reader reports both as '', so render_csv has nothing to
    distinguish and re-emits them as bare empty fields. This guards render_csv's
    own behaviour on the CSV forms it may receive, not psql's rendering: the
    NULL versus empty-string distinction is preserved upstream by psql's null
    marker (--pset=null=[NULL]), which turns NULL into the literal text
    "[NULL]" before this code runs and is covered by the integration test.
    """
    rendered = results.render_csv('a,b\n,""\n', max_rows=10)

    assert rendered.text == "a,b\n,"
