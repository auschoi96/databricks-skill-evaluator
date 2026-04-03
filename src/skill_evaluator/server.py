"""FastMCP server exposing skill evaluation as MCP tools.

Each evaluation level is a tool that Claude can call interactively.
The SKILL.md teaches Claude how to orchestrate these tools.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

from fastmcp import FastMCP

logger = logging.getLogger(__name__)

mcp = FastMCP("Skill Evaluator")


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------

def _build_level_config(
    skill_dir: str,
    mcp_json_path: Optional[str] = None,
    agent_model: Optional[str] = None,
    agent_timeout: int = 300,
    judge_model: Optional[str] = None,
):
    """Construct a LevelConfig from a skill directory and saved workspace config."""
    from .auth import load_config
    from .levels.base import LevelConfig
    from .mcp_resolver import MCPConfig
    from .skill_discovery import SkillDescriptor
    from .test_instructions import SkillTestInstructions

    skill_path = Path(skill_dir).resolve()
    skill = SkillDescriptor.from_directory(skill_path)
    test_instructions = SkillTestInstructions.from_skill_dir(skill_path)

    ws_config = load_config()
    if not ws_config:
        from .auth import WorkspaceConfig
        ws_config = WorkspaceConfig(
            profile="DEFAULT", host="", catalog="main", schema="default",
        )

    mcp_config = None
    if mcp_json_path:
        mcp_config = MCPConfig.from_mcp_json(Path(mcp_json_path))
    else:
        mcp_config = MCPConfig.auto_discover(skill_path)

    if mcp_config:
        mcp_config.resolve_available_tools()

    return LevelConfig(
        workspace=ws_config,
        skill=skill,
        test_instructions=test_instructions,
        mcp_config=mcp_config,
        agent_model=agent_model,
        agent_timeout=agent_timeout,
        judge_model=judge_model,
    )


def _safe_json(data, **kwargs) -> str:
    """JSON serialize with fallback for non-serializable objects."""
    return json.dumps(data, indent=2, default=str, **kwargs)


# ---------------------------------------------------------------------------
# Tool 1: Authentication
# ---------------------------------------------------------------------------

@mcp.tool()
def authenticate_workspace(
    profile: str = "DEFAULT",
    catalog: Optional[str] = None,
    schema: Optional[str] = None,
    warehouse_id: Optional[str] = None,
    experiment: Optional[str] = None,
) -> str:
    """Authenticate with a Databricks workspace and save config for subsequent tool calls.

    Call this first before running any evaluation levels. Validates connectivity,
    discovers SQL warehouses, and saves config to ~/.dse/config.yaml.

    Args:
        profile: Databricks CLI profile name from ~/.databrickscfg
        catalog: Unity Catalog to use for test resources
        schema: Schema to use for test resources
        warehouse_id: SQL warehouse ID (auto-detected if omitted)
        experiment: MLflow experiment path for logging results
    """
    try:
        from .auth import authenticate
        config = authenticate(
            profile=profile, catalog=catalog, schema=schema,
            warehouse_id=warehouse_id, experiment_path=experiment,
        )
        return _safe_json({
            "status": "authenticated",
            "profile": config.profile,
            "host": config.host,
            "catalog": config.catalog,
            "schema": config.schema,
            "warehouse_id": config.warehouse_id,
            "experiment_path": config.experiment_path,
        })
    except Exception as e:
        return _safe_json({"error": str(e), "error_type": type(e).__name__})


# ---------------------------------------------------------------------------
# Tool 2: Skill Discovery
# ---------------------------------------------------------------------------

@mcp.tool()
def discover_skill(skill_dir: str) -> str:
    """Discover and parse a Claude Code skill directory.

    Reads SKILL.md frontmatter, enumerates reference files, and detects
    MCP tool references. Use this to understand what a skill contains
    before running evaluation.

    Args:
        skill_dir: Absolute path to a directory containing SKILL.md
    """
    try:
        from .skill_discovery import SkillDescriptor
        skill = SkillDescriptor.from_directory(Path(skill_dir))
        return _safe_json({
            "name": skill.name,
            "description": skill.description,
            "path": str(skill.path),
            "reference_files": list(skill.reference_files.keys()),
            "mcp_tool_references": skill.mcp_tool_references,
            "has_eval_config": skill.has_eval_config,
            "frontmatter": skill.frontmatter,
        })
    except Exception as e:
        return _safe_json({"error": str(e), "error_type": type(e).__name__})


# ---------------------------------------------------------------------------
# Tool 3: Initialize Eval Config
# ---------------------------------------------------------------------------

@mcp.tool()
def init_eval_config(skill_dir: str) -> str:
    """Initialize evaluation config templates for a skill.

    Creates an eval/ directory with ground_truth.yaml, manifest.yaml,
    thinking_instructions.md, and output_instructions.md templates.
    The user must fill in the TODOs with their own test cases and criteria.

    Args:
        skill_dir: Absolute path to the skill directory
    """
    try:
        from .skill_discovery import SkillDescriptor
        from .test_instructions import init_eval_config as _init

        skill = SkillDescriptor.from_directory(Path(skill_dir))
        eval_dir = _init(Path(skill_dir), skill.name)

        files = [str(f.relative_to(eval_dir)) for f in sorted(eval_dir.rglob("*")) if f.is_file()]
        return _safe_json({
            "status": "initialized",
            "eval_dir": str(eval_dir),
            "files_created": files,
            "next_step": "Edit the TODO placeholders in ground_truth.yaml, thinking_instructions.md, and output_instructions.md with your own test cases and evaluation criteria.",
        })
    except Exception as e:
        return _safe_json({"error": str(e), "error_type": type(e).__name__})


# ---------------------------------------------------------------------------
# Tool 4: Level 1 — Unit Tests
# ---------------------------------------------------------------------------

@mcp.tool()
def run_unit_tests(
    skill_dir: str,
    mcp_json_path: Optional[str] = None,
) -> str:
    """Run Level 1 unit tests on a skill — validate syntax and tool availability.

    Extracts all fenced code blocks from SKILL.md and reference files,
    validates Python/SQL/YAML syntax, checks that referenced MCP tools
    actually exist in the MCP server, and checks for broken markdown links.
    No agent execution needed. Runs in seconds.

    Args:
        skill_dir: Absolute path to the skill directory
        mcp_json_path: Path to .mcp.json for tool verification (auto-discovers if omitted)
    """
    try:
        config = _build_level_config(skill_dir, mcp_json_path=mcp_json_path)
        from .levels.unit_tests import UnitTestLevel
        result = UnitTestLevel().run(config)
        return _safe_json(result.to_dict())
    except Exception as e:
        return _safe_json({"error": str(e), "error_type": type(e).__name__})


# ---------------------------------------------------------------------------
# Tool 5: Level 3 — Static Eval
# ---------------------------------------------------------------------------

@mcp.tool()
def run_static_eval(
    skill_dir: str,
    mcp_json_path: Optional[str] = None,
    judge_model: Optional[str] = None,
) -> str:
    """Run Level 3 static evaluation — LLM judge scores the SKILL.md quality.

    Evaluates the skill document itself (without execution) across 10 dimensions
    on a 1-10 scale: self-contained, no conflicts, security, LLM-navigable structure,
    actionable instructions, scoped clearly, tool accuracy, examples valid,
    error handling guidance, no hallucination triggers.

    Returns per-criteria scores, an overall score, and specific recommendations.
    Deterministic checks (tool accuracy, examples, secrets) run first at zero cost.

    Args:
        skill_dir: Absolute path to the skill directory
        mcp_json_path: Path to .mcp.json for tool accuracy verification (auto-discovers if omitted)
        judge_model: LLM model for semantic evaluation (default: databricks-claude-sonnet-4-6)
    """
    try:
        config = _build_level_config(skill_dir, mcp_json_path=mcp_json_path, judge_model=judge_model)
        from .levels.static_eval import StaticEvalLevel
        result = StaticEvalLevel().run(config)
        return _safe_json(result.to_dict())
    except Exception as e:
        return _safe_json({"error": str(e), "error_type": type(e).__name__})


# ---------------------------------------------------------------------------
# Tool 6: Level 2 — Integration Tests
# ---------------------------------------------------------------------------

@mcp.tool()
async def run_integration_tests(
    skill_dir: str,
    mcp_json_path: Optional[str] = None,
    agent_model: Optional[str] = None,
    agent_timeout: int = 300,
) -> str:
    """Run Level 2 integration tests — end-to-end against real Databricks.

    Executes the real Claude Code agent with the skill against your Databricks
    workspace. Tests MCP tool connectivity, runs test cases, and validates
    tool call success rates. Takes several minutes per test case.

    Args:
        skill_dir: Absolute path to the skill directory
        mcp_json_path: Path to .mcp.json with Databricks MCP server config (auto-discovered if omitted)
        agent_model: Claude model override for agent execution
        agent_timeout: Timeout in seconds per agent run (default: 300)
    """
    try:
        config = _build_level_config(skill_dir, mcp_json_path, agent_model, agent_timeout)
        from .levels.integration_tests import IntegrationTestLevel
        result = await asyncio.to_thread(IntegrationTestLevel().run, config)
        return _safe_json(result.to_dict())
    except Exception as e:
        return _safe_json({"error": str(e), "error_type": type(e).__name__})


# ---------------------------------------------------------------------------
# Tool 7: Level 4 — Thinking Eval
# ---------------------------------------------------------------------------

@mcp.tool()
async def run_thinking_eval(
    skill_dir: str,
    mcp_json_path: Optional[str] = None,
    agent_model: Optional[str] = None,
    agent_timeout: int = 300,
) -> str:
    """Run Level 4 thinking evaluation — assess agent reasoning quality.

    Evaluates HOW the agent reasons during execution: efficiency (tool call count),
    clarity (no confusion/backtracking), recovery (error handling), and completeness
    (all steps finished). Uses custom thinking_instructions.md from the skill's eval/ dir.

    Runs the real Claude Code agent. Takes several minutes per test case.

    Args:
        skill_dir: Absolute path to the skill directory
        mcp_json_path: Path to .mcp.json with Databricks MCP server config
        agent_model: Claude model override for agent execution
        agent_timeout: Timeout in seconds per agent run (default: 300)
    """
    try:
        config = _build_level_config(skill_dir, mcp_json_path, agent_model, agent_timeout)
        from .levels.thinking_eval import ThinkingEvalLevel
        result = await asyncio.to_thread(ThinkingEvalLevel().run, config)
        return _safe_json(result.to_dict())
    except Exception as e:
        return _safe_json({"error": str(e), "error_type": type(e).__name__})


# ---------------------------------------------------------------------------
# Tool 8: Level 5 — Output Eval
# ---------------------------------------------------------------------------

@mcp.tool()
async def run_output_eval(
    skill_dir: str,
    mcp_json_path: Optional[str] = None,
    agent_model: Optional[str] = None,
    agent_timeout: int = 300,
) -> str:
    """Run Level 5 output evaluation — WITH vs WITHOUT skill comparison.

    The core controlled experiment. For each test case, runs the agent WITH
    the skill and WITHOUT, then compares using the semantic grader. Classifies
    each assertion as POSITIVE (skill helps), REGRESSION (skill hurts),
    NEEDS_SKILL (both fail), or NEUTRAL (both pass).

    WITHOUT-skill baselines are cached across calls. Takes several minutes per test case.

    Args:
        skill_dir: Absolute path to the skill directory
        mcp_json_path: Path to .mcp.json with Databricks MCP server config
        agent_model: Claude model override for agent execution
        agent_timeout: Timeout in seconds per agent run (default: 300)
    """
    try:
        config = _build_level_config(skill_dir, mcp_json_path, agent_model, agent_timeout)
        from .levels.output_eval import OutputEvalLevel
        result = await asyncio.to_thread(OutputEvalLevel().run, config)
        return _safe_json(result.to_dict())
    except Exception as e:
        return _safe_json({"error": str(e), "error_type": type(e).__name__})


# ---------------------------------------------------------------------------
# Tool 9: Generate Report
# ---------------------------------------------------------------------------

@mcp.tool()
def generate_report(
    skill_dir: str,
    level_results_json: str,
) -> str:
    """Generate an HTML evaluation report from collected level results.

    Call this after running one or more evaluation levels. Pass the JSON
    results from each level as a combined dict. Generates a self-contained
    HTML report with per-level cards, scores, and a feedback export button.

    Args:
        skill_dir: Absolute path to the skill directory
        level_results_json: JSON string of {"level_name": level_result_dict, ...}
    """
    try:
        from .skill_discovery import SkillDescriptor
        from .levels.base import LevelResult
        from .orchestrator import _build_html_report, EvaluationSuiteConfig, EvaluationSuiteResult
        from .auth import load_config, WorkspaceConfig

        skill = SkillDescriptor.from_directory(Path(skill_dir))
        ws_config = load_config() or WorkspaceConfig(
            profile="DEFAULT", host="", catalog="", schema="",
        )

        level_data = json.loads(level_results_json)
        level_results = {}
        for name, data in level_data.items():
            level_results[name] = LevelResult(
                level=data.get("level", name),
                score=data.get("score", 0.0),
                feedbacks=data.get("feedbacks", []),
                task_results=data.get("task_results"),
                metadata=data.get("metadata"),
            )

        suite_result = EvaluationSuiteResult(
            skill_name=skill.name,
            level_results=level_results,
            composite_score=sum(r.score for r in level_results.values()) / len(level_results) if level_results else 0.0,
        )

        from .test_instructions import SkillTestInstructions
        suite_config = EvaluationSuiteConfig(
            workspace=ws_config,
            skill=skill,
            test_instructions=SkillTestInstructions.from_skill_dir(Path(skill_dir)),
        )

        html = _build_html_report(suite_config, suite_result)
        report_path = Path(skill_dir) / "eval" / "report.html"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(html)

        return _safe_json({
            "status": "report_generated",
            "report_path": str(report_path),
            "composite_score": suite_result.composite_score,
            "levels_included": list(level_results.keys()),
        })
    except Exception as e:
        return _safe_json({"error": str(e), "error_type": type(e).__name__})


# ---------------------------------------------------------------------------
# Tool 10: Optimization
# ---------------------------------------------------------------------------

@mcp.tool()
def run_optimization(
    skill_dir: str,
    preset: str = "quick",
    feedback_path: Optional[str] = None,
    apply: bool = False,
) -> str:
    """Run GEPA optimization to improve a SKILL.md based on evaluation results.

    Uses evolutionary optimization to iteratively mutate and test the skill content.
    Reads human feedback from feedback.json (exported from the HTML report) to guide
    the optimization direction.

    Args:
        skill_dir: Absolute path to the skill directory
        preset: Optimization intensity — minimal, quick, standard, or thorough
        feedback_path: Path to feedback.json from the HTML report review
        apply: If true, write the optimized SKILL.md back to disk immediately
    """
    return _safe_json({
        "status": "not_yet_implemented",
        "message": f"GEPA optimization with preset '{preset}' will be wired in a future update. "
                   "The optimize/ modules are extracted and ready — they need to be connected to this tool.",
        "preset": preset,
        "feedback_path": feedback_path,
        "apply": apply,
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    """Run the MCP server."""
    mcp.run(transport="stdio")
