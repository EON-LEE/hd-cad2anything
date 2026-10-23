"""Errors raised by domain tools."""

from __future__ import annotations

try:  # MCP surfaces only its own ToolError messages to the model; other exceptions become generic errors.
    from mcp.server.mcpserver.exceptions import ToolError as _BaseToolError
except ImportError:  # pragma: no cover - the toolkit also runs without the MCP extra
    _BaseToolError = ValueError


class ToolError(_BaseToolError):
    """Raised for invalid tool arguments or missing data; the message is shown to the agent."""


class NotFoundToolError(ToolError):
    """A ToolError for a missing resource (REST maps it to 404)."""
