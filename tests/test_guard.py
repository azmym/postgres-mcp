"""Tests for the read-only statement gate."""
from __future__ import annotations

from postgres_mcp import guard


def _scrub_checked(sql: str) -> str:
    """Scrub `sql`, asserting the alignment guarantees, and return the result.

    Every scrub case goes through this: asserting only that a keyword vanished
    would also hold if scrub returned an empty string, and the callers depend on
    exact alignment — split_statements slices the original by indices found in
    the scrubbed copy, and the meta-command scan maps scrubbed line N back to
    original line N.
    """
    scrubbed = guard.scrub(sql)

    assert len(scrubbed) == len(sql)
    assert [i for i, c in enumerate(scrubbed) if c == "\n"] == [
        i for i, c in enumerate(sql) if c == "\n"
    ]
    return scrubbed


def test_scrub_preserves_length_and_line_count() -> None:
    """Index alignment with the original is what lets us slice the raw SQL."""
    sql = "SELECT 1; -- a comment\nSELECT 2;"
    scrubbed = _scrub_checked(sql)

    assert len(scrubbed) == len(sql)
    assert scrubbed.count("\n") == sql.count("\n")


def test_scrub_blanks_line_comment_contents() -> None:
    sql = "SELECT 1 -- DELETE FROM t"
    assert "DELETE" not in _scrub_checked(sql)


def test_scrub_blanks_block_comment_contents() -> None:
    sql = "SELECT /* DROP TABLE t */ 1"
    assert "DROP" not in _scrub_checked(sql)


def test_scrub_blanks_nested_block_comment_contents() -> None:
    """Postgres block comments nest: the first */ closes the inner comment."""
    sql = "SELECT /* a /* DROP TABLE t */ b */ 1"
    scrubbed = _scrub_checked(sql)

    assert "DROP" not in scrubbed
    # The inner */ must not end the comment early, and the trailing 1 survives.
    assert scrubbed.split() == ["SELECT", "1"]


def test_scrub_blanks_string_literal_contents() -> None:
    sql = "SELECT 'DELETE FROM t'"
    assert "DELETE" not in _scrub_checked(sql)


def test_scrub_handles_doubled_quote_inside_literal() -> None:
    """'' is an escaped quote, not the end of the literal."""
    sql = "SELECT 'it''s DELETE', 1"
    assert "DELETE" not in _scrub_checked(sql)


def test_scrub_blanks_unterminated_literal_to_end() -> None:
    """An unclosed literal runs to end of input, so blank it all."""
    sql = "SELECT 'DELETE FROM t"
    assert "DELETE" not in _scrub_checked(sql)


def test_scrub_blanks_dollar_quoted_body() -> None:
    sql = "SELECT $$ INSERT INTO t VALUES (1) $$"
    assert "INSERT" not in _scrub_checked(sql)


def test_scrub_blanks_tagged_dollar_quoted_body() -> None:
    sql = "SELECT $body$ UPDATE t SET x = 1 $body$"
    assert "UPDATE" not in _scrub_checked(sql)


def test_scrub_blanks_unterminated_dollar_quote_to_end() -> None:
    sql = "SELECT $body$ UPDATE t SET x = 1"
    assert "UPDATE" not in _scrub_checked(sql)


def test_scrub_leaves_positional_parameters_alone() -> None:
    """$1 is a parameter placeholder, not a dollar-quote opener."""
    sql = "SELECT * FROM t WHERE id = $1"
    assert _scrub_checked(sql) == sql


def test_scrub_leaves_dollar_inside_identifier_alone() -> None:
    """Postgres reads a$b$c as one identifier, not a dollar quote.

    Treating it as one would blank everything after it, hiding whatever
    follows from the gate.
    """
    sql = "SELECT a$b$c, 1"
    assert _scrub_checked(sql) == sql


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


def test_meta_command_after_semicolon_rejected_read_write() -> None:
    """A \\! sharing a line with SQL is invisible to a line-start scan.

    Read-write mode has no other check to fall back on, and \\! runs a shell
    command on the client host, which no server-side setting can constrain.
    """
    result = guard.check(r"SELECT 1; \! rm -rf /", read_only=False)

    assert not result.allowed
    assert "meta-command" in result.reason


def test_meta_command_after_semicolon_rejected_read_only() -> None:
    """Rejected as a meta-command, not incidentally as unparseable SQL."""
    result = guard.check(r"SELECT 1; \! rm -rf /", read_only=True)

    assert not result.allowed
    assert "meta-command" in result.reason


def test_dollar_inside_identifier_does_not_hide_a_write() -> None:
    """Misreading a$b$c as a dollar quote would blank the separator and write.

    The gate would then approve statement text it never analysed.
    """
    result = guard.check("SELECT a$b$c, 1; DELETE FROM t", read_only=True)

    assert not result.allowed
    assert result.statement == "DELETE FROM t"


def test_meta_command_on_own_line_without_separator_rejected() -> None:
    """No semicolon, so this is one statement that opens with SELECT.

    Only the line scan sees the \\! here, which is why both scans exist.
    """
    result = guard.check("SELECT 1\n\\! rm -rf /", read_only=False)

    assert not result.allowed
    assert "meta-command" in result.reason
    assert result.statement == "\\! rm -rf /"


def test_meta_command_line_reported_despite_carriage_return() -> None:
    """A \\r inside a literal is blanked, so only "\\n" maps lines reliably."""
    result = guard.check("SELECT 'a\rb'\n\\! rm -rf /", read_only=False)

    assert not result.allowed
    assert result.statement == "\\! rm -rf /"


def test_line_comment_after_semicolon_allowed_read_only() -> None:
    """A trailing comment is not a statement, so it must not be gated."""
    assert guard.check("SELECT 1; -- note", read_only=True).allowed


def test_block_comment_after_semicolon_allowed_read_only() -> None:
    assert guard.check("SELECT 1; /* block comment */", read_only=True).allowed


def test_meta_command_survives_comment_filter_read_only() -> None:
    """Dropping a comment-only fragment must not drop a \\! on the next line."""
    result = guard.check("SELECT 1; -- note\n\\! rm -rf /", read_only=True)

    assert not result.allowed
    assert "meta-command" in result.reason


def test_meta_command_survives_comment_filter_read_write() -> None:
    result = guard.check("SELECT 1; -- note\n\\! rm -rf /", read_only=False)

    assert not result.allowed
    assert "meta-command" in result.reason


def test_comment_only_input_allowed_read_only() -> None:
    """A comment-only query has no statements and must not report nonsense."""
    result = guard.check("-- nothing here", read_only=True)

    assert result.allowed
    assert result.reason is None
