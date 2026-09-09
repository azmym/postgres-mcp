## What changed

<!-- A short description of the change. -->

## Why

<!-- The problem this solves, or the reason it is worth doing. -->

## Test

<!-- Which test proves this change, and the command that runs it. -->

## Full suite

<!-- Confirm the full 161-test run passed against a real server:

docker run --rm -d --name pg-mcp-test -e POSTGRES_PASSWORD=admin -p 5432:5432 postgres:18
PGHOST=localhost PGUSER=postgres PGPASSWORD=admin uv run pytest
docker rm -f pg-mcp-test

The ten integration tests are the only ones that run the pg_catalog queries
against a real parser and prove the read-only transaction refuses a write, so
do not merge with them still skipped. -->
