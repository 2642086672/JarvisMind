"""Native stdio MCP server for JarvisMind.

``python -m jarvismind mcp`` starts a dependency-free bridge that forwards MCP
tool calls to a running JarvisMind HTTP service.  Importing this package never
loads the backend, opens a database or reads a credential.
"""
from __future__ import annotations

from .config import MCPConfig
from .redaction import redact
from .server import MCPServer, run_stdio

__all__ = ["MCPConfig", "MCPServer", "run_stdio", "redact"]