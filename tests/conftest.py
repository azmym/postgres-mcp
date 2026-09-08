"""Shared pytest fixtures for postgres-mcp tests."""
from __future__ import annotations

import os

# Make @mcp.tool() return FunctionTool objects so tests can access .fn
os.environ.setdefault("FASTMCP_DECORATOR_MODE", "object")

import pytest
