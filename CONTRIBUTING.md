# Contributing

Thanks for wanting to help. This project is small and its whole point is a
security boundary, so the bar for a change is higher than the size of the diff
suggests.

## Set up

Python 3.11+ and `psql` on your PATH are the only requirements. `fastmcp` is
the only runtime dependency.

```bash
uv venv && uv pip install -e ".[dev]"
```

## Run the tests — both ways

There are two test modes, and the second one matters.

```bash
uv run pytest
```

Without a PostgreSQL server this gives **151 passed, 10 skipped**. The ten
skipped are the integration tests in `tests/test_integration.py`, and they are
the only tests that run the `pg_catalog` introspection queries against a real
parser, and the only ones that prove the read-only transaction genuinely
refuses a write. A defect that corrupted every query result once survived a
full review because these were skipped.

Run the full 161 against a throwaway server before opening a pull request:

```bash
docker run --rm -d --name pg-mcp-test \
  -e POSTGRES_PASSWORD=admin -p 5432:5432 postgres:18
PGHOST=localhost PGUSER=postgres PGPASSWORD=admin uv run pytest
docker rm -f pg-mcp-test
```

All 161 pass. Do not open a PR with the ten still skipped.

## Conventions

The code is the style guide. In particular:

- Tests come first; a change to behaviour is expected to carry the test that
  pins it.
- Every module starts with `from __future__ import annotations`.
- Functions are type-annotated.
- Docstrings explain *why* something exists, not what the line below does.

One exception: the tool docstrings in `server.py` are read by the calling model,
so they are interface, not commentary. Keep them accurate about what the tool
does and what it returns.

## Where the architecture lives

`docs/superpowers/specs/2026-09-08-postgres-mcp-design.md` holds the design and
its open risks. Read it before touching the read-only path.

## The read-only path

`postgres_mcp/guard.py` (the statement gate) and the `BEGIN READ ONLY` wrapper
in `postgres_mcp/psql.py` are the project's entire security claim. Any change
to that path must include a test proving the specific hole it closes is
actually closed — a change that "should" be safe without a test is not enough.
