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
