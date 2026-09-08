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


def test_select_into_rejected_read_only() -> None:
    """SELECT ... INTO creates a table, so it is not a read query."""
    result = guard.check("SELECT * INTO new_t FROM t", read_only=True)

    assert not result.allowed
    assert "INTO" in result.reason


def test_select_into_allowed_when_not_read_only() -> None:
    assert guard.check("SELECT * INTO new_t FROM t", read_only=False).allowed
