# Security Policy

postgres-mcp exists to be a security boundary: it hands a model a database and
promises that read-only means read-only. That promise is the product, so a
report that breaks it is the most valuable thing you can send us.

## The security model

Read-only is enforced in three independent layers, plus a fourth you should add
yourself:

1. **Backslash meta-commands are always rejected**, in both modes. `\!` runs a
   shell command on the host and never reaches the server, so no server-side
   control can cover it; `\copy` and `\o` write local files. Any statement
   opening with `\` is refused before psql is invoked.
2. **A statement gate** (`postgres_mcp/guard.py`) scrubs comments, string
   literals, quoted identifiers and dollar-quoted bodies, then requires every
   statement to open with one of `SELECT`, `WITH`, `EXPLAIN`, `SHOW`, `TABLE` or
   `VALUES`, and scans for the write verbs `INSERT`, `UPDATE`, `DELETE`, `MERGE`
   and `INTO` so a `WITH x AS (...) INSERT ...` cannot hide behind an allowed
   opener.
3. **`BEGIN READ ONLY`** (`postgres_mcp/psql.py`) wraps the statements, so
   PostgreSQL itself refuses a write even if the gate were fooled.

No tool accepts a `read_only` argument, so the calling model cannot escalate its
own privileges — the switch lives in configuration, not in the request.

The recommended fourth layer is a genuinely read-only PostgreSQL role (see the
README), which is the only layer that does not depend on this server's
correctness.

## What counts as a vulnerability

Report it if you find any of these:

- Any input that causes a write to reach a database configured read-only.
- Any way to execute a shell command or write a local file through a query.
- Any way to make the read-only decision loosen at runtime (a config value,
  environment variable, or request field that flips a database writable).
- Any leak of a configured password into process arguments, logs, or tool
  output.

## Known limits — not vulnerabilities

These are by design, so please do not report them:

- Read-only mode refuses `SET`, so `search_path` cannot be changed; schema-
  qualify your tables or use `describe_schema`.
- Read-only mode refuses row-locking reads (`SELECT ... FOR UPDATE` / `FOR
  SHARE`), because a read-only transaction rejects them anyway.
- Output is truncated by `max_rows` (default 1000) and a ~100 KB byte ceiling;
  the trailer names the real total.
- A column whose literal text is `[NULL]` is ambiguous with a real SQL NULL.

## Reporting

Prefer GitHub's **private vulnerability reporting** for this repository over a
public issue, so a fix can land before the details are public. If you cannot
use that path, `mahmoud.azmy@gmail.com` is a fallback — but private advisories
are preferred.

## Supported versions

This project is pre-1.0 with no tagged releases. The supported version is
`main`.
