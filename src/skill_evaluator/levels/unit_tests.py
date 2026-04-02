"""Level 1: Unit Tests (#404) — Code block syntax validation.

Validates that code examples in SKILL.md and reference files are
syntactically correct, and runs any user-provided pytest tests.
"""

from __future__ import annotations

import ast
import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from .base import EvalLevel, LevelConfig, LevelResult

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
            blocks = _extract_code_blocks(content)
            for i, (lang, code) in enumerate(blocks):
                block_id = f"{filename}:block_{i+1}"
                if lang in ("python", "py"):
                    result = _check_python_syntax(code)
                    feedbacks.append({
                        "name": f"unit/python_syntax/{block_id}",
                        "value": "pass" if result["valid"] else "fail",
                        "rationale": result.get("error", "Valid Python syntax"),
                        "source": "CODE",
                    })
                elif lang in ("sql",):
                    result = _check_sql_syntax(code)
                    feedbacks.append({
                        "name": f"unit/sql_syntax/{block_id}",
                        "value": "pass" if result["valid"] else "fail",
                        "rationale": result.get("error", "Valid SQL syntax"),
                        "source": "CODE",
                    })
                elif lang in ("yaml", "yml"):
                    result = _check_yaml_syntax(code)
                    feedbacks.append({
                        "name": f"unit/yaml_syntax/{block_id}",
                        "value": "pass" if result["valid"] else "fail",
                        "rationale": result.get("error", "Valid YAML syntax"),
                        "source": "CODE",
                    })

        # 2. Check for broken relative links between .md files
        link_results = _check_markdown_links(config.skill.path, all_content)
        feedbacks.extend(link_results)

        # 3. Run pytest if eval/tests/ directory exists
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


def _extract_code_blocks(markdown: str) -> list[tuple[str, str]]:
    """Extract fenced code blocks with their language from markdown."""
    pattern = r"```(\w*)\n(.*?)```"
    blocks = []
    for match in re.finditer(pattern, markdown, re.DOTALL):
        lang = match.group(1).lower() or "unknown"
        code = match.group(2).strip()
        if code:  # Skip empty blocks
            blocks.append((lang, code))
    return blocks


def _check_python_syntax(code: str) -> dict[str, Any]:
    """Validate Python syntax using ast.parse."""
    try:
        ast.parse(code)
        return {"valid": True}
    except SyntaxError as e:
        return {"valid": False, "error": f"SyntaxError at line {e.lineno}: {e.msg}"}


def _check_sql_syntax(code: str) -> dict[str, Any]:
    """Basic SQL syntax validation.

    Checks for balanced parentheses and common structural issues.
    Not a full SQL parser — catches obvious errors.
    """
    # Check balanced parentheses
    depth = 0
    for char in code:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if depth < 0:
            return {"valid": False, "error": "Unbalanced parentheses: extra ')'"}

    if depth != 0:
        return {"valid": False, "error": f"Unbalanced parentheses: {depth} unclosed '('"}

    # Check for common issues
    stripped = code.strip()
    if not stripped:
        return {"valid": False, "error": "Empty SQL block"}

    return {"valid": True}


def _check_yaml_syntax(code: str) -> dict[str, Any]:
    """Validate YAML syntax using yaml.safe_load."""
    try:
        yaml.safe_load(code)
        return {"valid": True}
    except yaml.YAMLError as e:
        return {"valid": False, "error": f"YAML error: {e}"}


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
