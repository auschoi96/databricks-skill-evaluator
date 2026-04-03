"""MCP server configuration resolver.

Resolves MCP server configurations so the Claude Agent SDK can start
the required MCP servers for a skill's tools.
"""

from __future__ import annotations

import ast
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

    def resolve_available_tools(self) -> None:
        """Populate available_tools by statically parsing MCP server source code.

        For each server whose entry point is a local Python file, follows imports
        to find @mcp.tool decorated functions via AST parsing. No subprocess or
        MCP connections — purely file reads + ast.parse.

        Silently skips servers that can't be parsed (non-Python, missing files, etc.).
        """
        tools: list[str] = []

        for server_name, config in self.servers.items():
            args = config.get("args", [])
            command = config.get("command", "")

            # Find the Python entry point (.py file in args)
            entry_point = None
            for arg in args:
                if isinstance(arg, str) and arg.endswith(".py"):
                    entry_point = Path(arg)
                    break

            # Also check if command itself is a .py file
            if entry_point is None and isinstance(command, str) and command.endswith(".py"):
                entry_point = Path(command)

            if entry_point is None or not entry_point.exists():
                continue

            try:
                server_tools = _extract_tools_from_entry_point(entry_point)
                for tool_name in server_tools:
                    tools.append(f"mcp__{server_name}__{tool_name}")
            except Exception as e:
                logger.debug(f"Could not extract tools from {server_name}: {e}")

        if tools:
            self.available_tools = sorted(tools)
            logger.info(f"Resolved {len(tools)} available MCP tools from server source")


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

    # Replace Claude Code project variables with base_dir
    value = value.replace("${CLAUDE_PLUGIN_ROOT}", str(base_dir))
    value = value.replace("${CLAUDE_PROJECT_ROOT}", str(base_dir))

    # Replace ${VAR} and ${VAR:-default}
    return re.sub(r"\$\{([^}]+)\}", replacer, value)


def _extract_tools_from_entry_point(entry_point: Path) -> list[str]:
    """Extract @mcp.tool function names by following imports from an entry point.

    Walks: entry_point.py → server module → tool modules → @mcp.tool functions.
    """
    entry_source = entry_point.read_text()
    entry_tree = ast.parse(entry_source)
    entry_dir = entry_point.parent

    # Find the server module: look for "from <package>.server import mcp"
    # or "from <package> import server" patterns
    server_module_path = _find_server_module(entry_tree, entry_dir)
    if server_module_path is None:
        # Fallback: the entry point itself might register tools
        return _extract_mcp_tool_names(entry_source)

    server_source = server_module_path.read_text()
    server_tree = ast.parse(server_source)
    server_dir = server_module_path.parent

    # Collect tools defined directly in server module
    tools = _extract_mcp_tool_names(server_source)

    # Find tool module imports: "from .tools import sql, compute, genie, ..."
    for node in ast.walk(server_tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.module and ("tools" in node.module):
            # Resolve the tools package directory
            # e.g., ".tools" relative to server.py's package
            tools_dir = server_dir / "tools"
            if not tools_dir.is_dir():
                continue
            for alias in node.names:
                mod_name = alias.name
                mod_path = tools_dir / f"{mod_name}.py"
                if mod_path.exists():
                    try:
                        mod_source = mod_path.read_text()
                        tools.extend(_extract_mcp_tool_names(mod_source))
                    except Exception:
                        pass

    return tools


def _find_server_module(entry_tree: ast.AST, entry_dir: Path) -> Path | None:
    """Find the server.py module referenced by the entry point."""
    for node in ast.walk(entry_tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.module and "server" in node.module:
            # Convert dotted module path to file path
            # e.g., "databricks_mcp_server.server" → "databricks_mcp_server/server.py"
            parts = node.module.split(".")
            candidate = entry_dir / Path(*parts).with_suffix(".py")
            if candidate.exists():
                return candidate
            # Try as package: databricks_mcp_server/server.py
            # Search both entry_dir and entry_dir/src (for src-layout projects)
            search_roots = [entry_dir]
            src_dir = entry_dir / "src"
            if src_dir.is_dir():
                search_roots.append(src_dir)
            for root in search_roots:
                for subdir in root.iterdir():
                    if subdir.is_dir():
                        candidate = subdir / "server.py"
                        if candidate.exists():
                            # Verify this module matches the import
                            if parts[-1] == "server" and subdir.name == parts[0]:
                                return candidate
    return None


def _extract_mcp_tool_names(source: str) -> list[str]:
    """Extract function names decorated with @mcp.tool from Python source."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    tools = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if _is_mcp_tool_decorator(decorator):
                tools.append(node.name)
                break
    return tools


def _is_mcp_tool_decorator(node: ast.expr) -> bool:
    """Check if a decorator AST node is @mcp.tool or @mcp.tool(...)."""
    # @mcp.tool
    if isinstance(node, ast.Attribute):
        return (
            isinstance(node.value, ast.Name)
            and node.value.id == "mcp"
            and node.attr == "tool"
        )
    # @mcp.tool() or @mcp.tool(timeout=60)
    if isinstance(node, ast.Call):
        return _is_mcp_tool_decorator(node.func)
    return False


class MCPResolverError(Exception):
    """Raised when MCP configuration resolution fails."""
    pass
