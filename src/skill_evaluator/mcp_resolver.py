"""MCP server configuration resolver.

Resolves MCP server configurations so the Claude Agent SDK can start
the required MCP servers for a skill's tools.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class MCPConfig:
    """Resolved MCP server configuration."""

    servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    available_tools: list[str] = field(default_factory=list)

    @classmethod
    def from_mcp_json(cls, path: Path) -> "MCPConfig":
        """Load MCP config from a .mcp.json file.

        Resolves ${VAR} and ${VAR:-default} patterns in the config.
        """
        path = Path(path).resolve()
        if not path.exists():
            raise MCPResolverError(f"MCP config not found: {path}")

        with open(path) as f:
            data = json.load(f)

        servers = data.get("mcpServers", {})
        resolved = {}

        for name, config in servers.items():
            resolved[name] = _resolve_env_vars(config, path.parent)

        return cls(servers=resolved)

    @classmethod
    def from_server_command(cls, name: str, command: str, args: list[str] | None = None) -> "MCPConfig":
        """Create config from a manual server specification.

        Args:
            name: Server name (e.g., "databricks")
            command: Command to start the server (e.g., "/path/to/python")
            args: Command arguments (e.g., ["/path/to/run_server.py"])
        """
        servers = {
            name: {
                "command": command,
                "args": args or [],
            }
        }
        return cls(servers=servers)

    @classmethod
    def auto_discover(cls, start_dir: Path) -> Optional["MCPConfig"]:
        """Walk up from start_dir looking for .mcp.json.

        Returns None if no .mcp.json is found.
        """
        current = Path(start_dir).resolve()
        for _ in range(10):  # Limit depth
            candidate = current / ".mcp.json"
            if candidate.exists():
                logger.info(f"Auto-discovered MCP config: {candidate}")
                return cls.from_mcp_json(candidate)
            parent = current.parent
            if parent == current:
                break
            current = parent
        return None

    def inject_env(self, env_vars: dict[str, str]) -> None:
        """Inject additional environment variables into all servers."""
        for name, config in self.servers.items():
            config.setdefault("env", {})
            config["env"].update(env_vars)


def _resolve_env_vars(config: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    """Resolve ${VAR} and ${VAR:-default} patterns in config values.

    Also resolves ${CLAUDE_PLUGIN_ROOT} to base_dir.
    """
    resolved = {}
    for key, value in config.items():
        if isinstance(value, str):
            resolved[key] = _resolve_string(value, base_dir)
        elif isinstance(value, list):
            resolved[key] = [_resolve_string(v, base_dir) if isinstance(v, str) else v for v in value]
        elif isinstance(value, dict):
            resolved[key] = _resolve_env_vars(value, base_dir)
        else:
            resolved[key] = value
    return resolved


def _resolve_string(value: str, base_dir: Path) -> str:
    """Resolve environment variable references in a string."""
    def replacer(match):
        var_expr = match.group(1)
        if ":-" in var_expr:
            var_name, default = var_expr.split(":-", 1)
            return os.environ.get(var_name, default)
        return os.environ.get(var_expr, match.group(0))

    # Replace ${CLAUDE_PLUGIN_ROOT} with base_dir
    value = value.replace("${CLAUDE_PLUGIN_ROOT}", str(base_dir))

    # Replace ${VAR} and ${VAR:-default}
    return re.sub(r"\$\{([^}]+)\}", replacer, value)


class MCPResolverError(Exception):
    """Raised when MCP configuration resolution fails."""
    pass
