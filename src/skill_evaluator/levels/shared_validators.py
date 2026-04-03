"""Shared validation utilities used across evaluation levels.

Pure, stateless functions for:
- Extracting and validating code blocks from markdown (L1/L3)
- Checking trace expectations against agent execution traces (L2/L4)
- Converting between MLflow Feedback objects and level feedback dicts
"""

from __future__ import annotations

import ast
import re
from typing import Any, TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from ..core.trace_models import TraceMetrics

# ──────────────────────────────────────────────────────────────────
# MLflow Feedback ↔ Level dict conversion
# ──────────────────────────────────────────────────────────────────

# Value mapping between level convention and MLflow convention
MLFLOW_TO_LEVEL = {"yes": "pass", "no": "fail", None: "skip", "skip": "skip"}
LEVEL_TO_MLFLOW = {"pass": "yes", "fail": "no", "skip": None}


def feedback_to_dict(
    fb: Any,
    level_prefix: str,
    case_id: str,
) -> dict[str, Any]:
    """Convert an MLflow Feedback object to the dict format levels use.

    Args:
        fb: An mlflow.entities.Feedback object with name, value, rationale.
        level_prefix: Feedback name prefix (e.g. "integration" or "thinking").
        case_id: Test case identifier.

    Returns:
        Dict with name, value, rationale, source keys.
    """
    mlflow_value = fb.value if hasattr(fb, "value") else str(fb)
    level_value = MLFLOW_TO_LEVEL.get(mlflow_value, "fail")
    return {
        "name": f"{level_prefix}/{case_id}/{fb.name}",
        "value": level_value,
        "rationale": getattr(fb, "rationale", "") or "",
        "source": "SCORER",
    }


def dict_to_feedback(fb_dict: dict[str, Any]) -> Any:
    """Convert a level feedback dict to an MLflow Feedback object.

    Returns None if mlflow is not available.
    """
    try:
        from mlflow.entities import Feedback
    except ImportError:
        return None

    value = LEVEL_TO_MLFLOW.get(fb_dict.get("value", ""), fb_dict.get("value"))
    return Feedback(
        name=fb_dict.get("name", "unknown"),
        value=value,
        rationale=fb_dict.get("rationale", ""),
    )


def extract_code_blocks(markdown: str) -> list[tuple[str, str]]:
    """Extract fenced code blocks with their language from markdown."""
    pattern = r"```(\w*)\n(.*?)```"
    blocks = []
    for match in re.finditer(pattern, markdown, re.DOTALL):
        lang = match.group(1).lower() or "unknown"
        code = match.group(2).strip()
        if code:  # Skip empty blocks
            blocks.append((lang, code))
    return blocks


def check_python_syntax(code: str) -> dict[str, Any]:
    """Validate Python syntax using ast.parse."""
    try:
        ast.parse(code)
        return {"valid": True}
    except SyntaxError as e:
        return {"valid": False, "error": f"SyntaxError at line {e.lineno}: {e.msg}"}


def check_sql_syntax(code: str) -> dict[str, Any]:
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


def check_yaml_syntax(code: str) -> dict[str, Any]:
    """Validate YAML syntax using yaml.safe_load."""
    try:
        yaml.safe_load(code)
        return {"valid": True}
    except yaml.YAMLError as e:
        return {"valid": False, "error": f"YAML error: {e}"}


def check_trace_expectations(
    case_id: str,
    trace: "TraceMetrics",
    expectations: dict[str, Any],
    level_prefix: str,
) -> list[dict[str, Any]]:
    """Check trace-based expectations from ground_truth.yaml.

    Shared between L2 (integration) and L4 (thinking) to avoid duplicate
    code for required_tools, banned_tools, and tool_limits checks.

    Delegates to the MLflow @scorer functions in scorers/trace.py when
    available, falling back to inline logic otherwise. This ensures the
    scorers are exercised (not dead code) while maintaining compatibility.

    Args:
        case_id: Test case identifier.
        trace: TraceMetrics from agent execution.
        expectations: The full expectations dict from the test case.
        level_prefix: Feedback name prefix (e.g. "integration" or "thinking").

    Returns:
        List of feedback dicts with pass/fail results.
    """
    trace_exp = expectations.get("trace_expectations", {})

    # Try delegating to MLflow @scorer functions
    try:
        return _check_trace_via_scorers(case_id, trace, trace_exp, level_prefix)
    except Exception:
        # Fallback to inline logic if scorers are unavailable
        return _check_trace_inline(case_id, trace, trace_exp, level_prefix)


def _check_trace_via_scorers(
    case_id: str,
    trace: "TraceMetrics",
    trace_exp: dict[str, Any],
    level_prefix: str,
) -> list[dict[str, Any]]:
    """Delegate trace checks to MLflow @scorer functions from scorers/trace.py."""
    from ..scorers.trace import required_tools as rt_scorer
    from ..scorers.trace import banned_tools as bt_scorer
    from ..scorers.trace import tool_count as tc_scorer

    feedbacks: list[dict[str, Any]] = []
    trace_dict = trace.to_dict()

    # Required tools via @scorer
    if trace_exp.get("required_tools"):
        fb = rt_scorer(trace=trace_dict, expectations=trace_exp)
        if hasattr(fb, "value") and fb.value != "skip":
            # The scorer returns a single Feedback — expand to per-tool for granularity
            for tool in trace_exp["required_tools"]:
                found = trace.has_tool(tool)
                feedbacks.append({
                    "name": f"{level_prefix}/{case_id}/required_tool/{tool}",
                    "value": "pass" if found else "fail",
                    "rationale": f"Required tool '{tool}' {'used' if found else 'NOT used'}",
                    "source": "SCORER",
                })

    # Banned tools via @scorer
    if trace_exp.get("banned_tools"):
        fb = bt_scorer(trace=trace_dict, expectations=trace_exp)
        if hasattr(fb, "value") and fb.value != "skip":
            for tool in trace_exp["banned_tools"]:
                used = trace.has_tool(tool)
                feedbacks.append({
                    "name": f"{level_prefix}/{case_id}/banned_tool/{tool}",
                    "value": "pass" if not used else "fail",
                    "rationale": f"Banned tool '{tool}' {'NOT used (good)' if not used else 'was USED'}",
                    "source": "SCORER",
                })

    # Tool limits via @scorer
    if trace_exp.get("tool_limits"):
        fb = tc_scorer(trace=trace_dict, expectations=trace_exp)
        if hasattr(fb, "value") and fb.value != "skip":
            for tool_name, max_count in trace_exp["tool_limits"].items():
                actual = trace.get_tool_count(tool_name)
                within = actual <= max_count
                feedbacks.append({
                    "name": f"{level_prefix}/{case_id}/tool_limit/{tool_name}",
                    "value": "pass" if within else "fail",
                    "rationale": f"Tool '{tool_name}': {actual} calls (limit: {max_count})",
                    "source": "SCORER",
                })

    return feedbacks


def _check_trace_inline(
    case_id: str,
    trace: "TraceMetrics",
    trace_exp: dict[str, Any],
    level_prefix: str,
) -> list[dict[str, Any]]:
    """Fallback inline trace checks when scorers are unavailable."""
    feedbacks: list[dict[str, Any]] = []

    for tool in trace_exp.get("required_tools", []):
        found = trace.has_tool(tool)
        feedbacks.append({
            "name": f"{level_prefix}/{case_id}/required_tool/{tool}",
            "value": "pass" if found else "fail",
            "rationale": f"Required tool '{tool}' {'used' if found else 'NOT used'}",
            "source": "CODE",
        })

    for tool in trace_exp.get("banned_tools", []):
        used = trace.has_tool(tool)
        feedbacks.append({
            "name": f"{level_prefix}/{case_id}/banned_tool/{tool}",
            "value": "pass" if not used else "fail",
            "rationale": f"Banned tool '{tool}' {'NOT used (good)' if not used else 'was USED'}",
            "source": "CODE",
        })

    for tool_name, max_count in trace_exp.get("tool_limits", {}).items():
        actual = trace.get_tool_count(tool_name)
        within = actual <= max_count
        feedbacks.append({
            "name": f"{level_prefix}/{case_id}/tool_limit/{tool_name}",
            "value": "pass" if within else "fail",
            "rationale": f"Tool '{tool_name}': {actual} calls (limit: {max_count})",
            "source": "CODE",
        })

    return feedbacks
