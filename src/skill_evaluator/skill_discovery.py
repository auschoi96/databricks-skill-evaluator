"""Skill directory parser and validator.

Discovers and validates a user-provided skill directory by parsing
SKILL.md frontmatter, enumerating reference files, and detecting
MCP tool references.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass
class SkillDescriptor:
    """Parsed representation of a Claude Code skill directory."""

    name: str
    description: str
    path: Path
    skill_md_content: str
    reference_files: dict[str, str] = field(default_factory=dict)
    mcp_tool_references: list[str] = field(default_factory=list)
    frontmatter: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_directory(cls, skill_dir: Path) -> "SkillDescriptor":
        """Parse a skill directory.

        Args:
            skill_dir: Path to a directory containing SKILL.md.

        Raises:
            SkillDiscoveryError: If SKILL.md is missing or has invalid frontmatter.
        """
        skill_dir = Path(skill_dir).resolve()

        skill_md_path = skill_dir / "SKILL.md"
        if not skill_md_path.exists():
            raise SkillDiscoveryError(f"No SKILL.md found in {skill_dir}")

        skill_md_content = skill_md_path.read_text()

        # Parse frontmatter
        frontmatter = _parse_frontmatter(skill_md_content)
        name = frontmatter.get("name", skill_dir.name)
        description = frontmatter.get("description", "")

        if not name:
            raise SkillDiscoveryError(
                f"SKILL.md in {skill_dir} missing 'name' in frontmatter"
            )

        # Enumerate reference files (all .md files except SKILL.md)
        reference_files = {}
        for md_file in sorted(skill_dir.glob("*.md")):
            if md_file.name == "SKILL.md":
                continue
            reference_files[md_file.name] = md_file.read_text()

        # Also check subdirectories one level deep for .md files
        for subdir in sorted(skill_dir.iterdir()):
            if subdir.is_dir() and subdir.name not in ("eval", "tests", "__pycache__", ".git"):
                for md_file in sorted(subdir.glob("*.md")):
                    rel_path = f"{subdir.name}/{md_file.name}"
                    reference_files[rel_path] = md_file.read_text()

        # Detect MCP tool references
        all_content = skill_md_content + "\n" + "\n".join(reference_files.values())
        mcp_tool_references = _extract_mcp_tool_references(all_content)

        logger.info(
            f"Discovered skill '{name}' with {len(reference_files)} reference files "
            f"and {len(mcp_tool_references)} MCP tool references"
        )

        return cls(
            name=name,
            description=description,
            path=skill_dir,
            skill_md_content=skill_md_content,
            reference_files=reference_files,
            mcp_tool_references=mcp_tool_references,
            frontmatter=frontmatter,
        )

    @property
    def all_content(self) -> str:
        """Full skill content (SKILL.md + all reference files)."""
        parts = [self.skill_md_content]
        for name, content in self.reference_files.items():
            parts.append(f"\n\n--- {name} ---\n\n{content}")
        return "\n".join(parts)

    @property
    def has_eval_config(self) -> bool:
        """Check if this skill has an eval/ directory with configuration."""
        return (self.path / "eval").is_dir()


def _parse_frontmatter(content: str) -> dict[str, Any]:
    """Parse YAML frontmatter from a markdown file.

    Expects content starting with '---' followed by YAML, closed by '---'.
    """
    if not content.startswith("---"):
        return {}

    # Find closing ---
    end_idx = content.find("---", 3)
    if end_idx == -1:
        return {}

    frontmatter_text = content[3:end_idx].strip()
    try:
        return yaml.safe_load(frontmatter_text) or {}
    except yaml.YAMLError:
        return {}


def _extract_mcp_tool_references(content: str) -> list[str]:
    """Extract MCP tool names referenced in skill content.

    Detects patterns like:
    - mcp__databricks__tool_name
    - `tool_name` in MCP tool tables
    - Tool names in backticks after "Tool |" table headers
    """
    tools = set()

    # Pattern 1: Full MCP tool names (mcp__server__tool)
    for match in re.finditer(r"mcp__(\w+)__(\w+)", content):
        tools.add(match.group(2))  # Just the tool name

    # Pattern 2: Tool names in markdown tables (| `tool_name` | ...)
    for match in re.finditer(r"\|\s*`(\w+)`\s*\|", content):
        tool = match.group(1)
        # Filter out common non-tool words
        if not tool.startswith(("int", "str", "bool", "float", "dict", "list")):
            tools.add(tool)

    # Pattern 3: Tool names after function-call patterns
    for match in re.finditer(r"(\w+)\(\s*\n?\s*\w+\s*=", content):
        candidate = match.group(1)
        # Only include if it looks like a tool name (snake_case, not a Python builtin)
        if "_" in candidate and candidate.islower() and len(candidate) > 5:
            tools.add(candidate)

    return sorted(tools)


class SkillDiscoveryError(Exception):
    """Raised when skill directory parsing fails."""
    pass
