"""Level 1: Unit Tests (#404) — Code block syntax and tool availability validation.

Validates that code examples in SKILL.md and reference files are
syntactically correct, checks that referenced MCP tools exist in the
MCP server, and runs any user-provided pytest tests.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from .base import EvalLevel, LevelConfig, LevelResult
from .shared_validators import (
    extract_code_blocks,
    check_python_syntax,
    check_sql_syntax,
    check_yaml_syntax,
)

logger = logging.getLogger(__name__)


class UnitTestLevel(EvalLevel):
    """Validate skill code blocks and run pytest tests."""

    @property
    def name(self) -> str:
        return "unit"

    @property
    def level_number(self) -> int:
        return 1

    def run(self, config: LevelConfig) -> LevelResult:
        feedbacks: list[dict[str, Any]] = []

        # 1. Extract and validate code blocks from SKILL.md + references
        all_content = {"SKILL.md": config.skill.skill_md_content}
        all_content.update(config.skill.reference_files)

        for filename, content in all_content.items():
            blocks = extract_code_blocks(content)
            for i, (lang, code) in enumerate(blocks):
                block_id = f"{filename}:block_{i+1}"
                if lang in ("python", "py"):
                    result = check_python_syntax(code)
                    feedbacks.append({
                        "name": f"unit/python_syntax/{block_id}",
                        "value": "pass" if result["valid"] else "fail",
                        "rationale": result.get("error", "Valid Python syntax"),
                        "source": "CODE",
                    })
                elif lang in ("sql",):
                    result = check_sql_syntax(code)
                    feedbacks.append({
                        "name": f"unit/sql_syntax/{block_id}",
                        "value": "pass" if result["valid"] else "fail",
                        "rationale": result.get("error", "Valid SQL syntax"),
                        "source": "CODE",
                    })
                elif lang in ("yaml", "yml"):
                    result = check_yaml_syntax(code)
                    feedbacks.append({
                        "name": f"unit/yaml_syntax/{block_id}",
                        "value": "pass" if result["valid"] else "fail",
                        "rationale": result.get("error", "Valid YAML syntax"),
                        "source": "CODE",
                    })

        # 2. Check for broken relative links between .md files
        link_results = _check_markdown_links(config.skill.path, all_content)
        feedbacks.extend(link_results)

        # 3. Check that referenced MCP tools actually exist
        tool_feedbacks = _check_tool_references(config)
        feedbacks.extend(tool_feedbacks)

        # 4. Run pytest if eval/tests/ directory exists
        test_dir = config.skill.path / "eval" / "tests"
        if test_dir.is_dir():
            pytest_results = _run_pytest(test_dir)
            feedbacks.extend(pytest_results)

        # Compute score
        total = len(feedbacks)
        passed = sum(1 for f in feedbacks if f["value"] == "pass")
        score = passed / total if total > 0 else 1.0

        return LevelResult(
            level=self.name,
            score=score,
            feedbacks=feedbacks,
            metadata={
                "code_blocks_tested": sum(
                    1 for f in feedbacks if f["name"].startswith("unit/")
                    and not f["name"].startswith("unit/link")
                    and not f["name"].startswith("unit/pytest")
                ),
                "syntax_errors": sum(1 for f in feedbacks if f["value"] == "fail"),
            },
        )


def _check_markdown_links(skill_dir: Path, files: dict[str, str]) -> list[dict[str, Any]]:
    """Check for broken relative links in markdown files."""
    results = []
    link_pattern = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

    for filename, content in files.items():
        for match in link_pattern.finditer(content):
            link_text, link_target = match.group(1), match.group(2)

            # Skip external links and anchors
            if link_target.startswith(("http://", "https://", "#", "mailto:")):
                continue

            # Strip anchor from path
            path_part = link_target.split("#")[0]
            if not path_part:
                continue

            target_path = skill_dir / path_part
            is_valid = target_path.exists()

            results.append({
                "name": f"unit/link/{filename}/{link_text}",
                "value": "pass" if is_valid else "fail",
                "rationale": f"Link to '{link_target}' {'exists' if is_valid else 'not found'}",
                "source": "CODE",
            })

    return results


def _check_tool_references(config) -> list[dict[str, Any]]:
    """Check that MCP tools referenced in the skill actually exist.

    Uses two sources of tool references:
    1. Explicit mcp__server__tool patterns from skill content (strongest signal)
    2. Bare tool names from skill_discovery that match known MCP tool suffixes

    Gracefully skips if MCP config or available tools aren't populated.
    """
    results = []

    if not config.mcp_config or not config.mcp_config.available_tools:
        # Only emit skip if there are tool references to check
        if config.skill.mcp_tool_references:
            results.append({
                "name": "unit/tool_available",
                "value": "skip",
                "rationale": "No MCP tools available for verification (MCP server not resolved)",
                "source": "CODE",
            })
        return results

    available = set(config.mcp_config.available_tools)
    # Build a set of bare tool names (suffix after last __) for matching
    available_bare = {t.split("__")[-1] for t in available if "__" in t}

    all_content = config.skill.all_content
    tools_to_check: dict[str, str] = {}  # display_name -> full_name_or_bare

    # Source 1: Explicit mcp__server__tool patterns (highest confidence)
    for match in re.finditer(r"mcp__(\w+)__(\w+)", all_content):
        full_name = match.group(0)
        bare_name = match.group(2)
        tools_to_check[bare_name] = full_name

    # Source 2: Bare tool names from skill_discovery that are actual MCP tools
    # Only include names that exist in OR plausibly could be in the available set
    # (i.e., they match the naming pattern of real tools — contain underscore)
    for tool in config.skill.mcp_tool_references:
        if tool in tools_to_check:
            continue  # Already covered by Source 1
        # Only check bare names that look like real tool names and exist
        # in the available bare set. This filters out parameter names like
        # "space_id", "warehouse_id" that the heuristic extractor picks up.
        if tool in available_bare:
            tools_to_check[tool] = tool

    if not tools_to_check:
        return results

    for bare_name in sorted(tools_to_check):
        full_or_bare = tools_to_check[bare_name]
        if full_or_bare.startswith("mcp__"):
            # Explicit reference — check exact match
            found = full_or_bare in available
        else:
            # Bare name — check if any available tool ends with it
            found = bare_name in available_bare

        results.append({
            "name": f"unit/tool_available/{bare_name}",
            "value": "pass" if found else "fail",
            "rationale": f"Tool '{bare_name}' {'found' if found else 'NOT found'} in MCP server",
            "source": "CODE",
        })

    return results


def _run_pytest(test_dir: Path) -> list[dict[str, Any]]:
    """Run pytest on a test directory and return structured results."""
    results = []
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", str(test_dir), "--tb=short", "-q"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        passed = proc.returncode == 0
        results.append({
            "name": "unit/pytest/suite",
            "value": "pass" if passed else "fail",
            "rationale": proc.stdout[-500:] if proc.stdout else proc.stderr[-500:],
            "source": "CODE",
        })
    except subprocess.TimeoutExpired:
        results.append({
            "name": "unit/pytest/suite",
            "value": "fail",
            "rationale": "pytest timed out after 120 seconds",
            "source": "CODE",
        })
    except Exception as e:
        results.append({
            "name": "unit/pytest/suite",
            "value": "fail",
            "rationale": f"Failed to run pytest: {e}",
            "source": "CODE",
        })

    return results
