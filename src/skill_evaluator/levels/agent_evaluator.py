"""Agent-based evaluator: run real Claude Code agent and score behavior.

Extracted from ai-dev-kit/.test/src/skill_test/optimize/agent_evaluator.py

GEPA-compatible evaluator that runs a Claude Code instance via the Agent SDK,
captures the full execution trace, and scores using a semantic assertion
grader (hybrid deterministic + LLM) plus deterministic trace scorers.

Scoring matches evaluate.py: compute_score(diagnostics) with defaults
(token_efficiency=1.0, structure=1.0). Behavioral scorers and
execution_success are still computed for GEPA reflection context
but do not affect the final score.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from typing import Any, Callable

from ..agent.executor import AgentResult, run_agent_sync_wrapper
from ..scorers.trace import (
    required_tools as required_tools_scorer,
    banned_tools as banned_tools_scorer,
    tool_sequence as tool_sequence_scorer,
)
from ..grading.semantic_grader import (
    grade_with_without,
    build_side_info,
    compute_score,
)
from ..optimize.utils import count_tokens

logger = logging.getLogger(__name__)


def _prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode()).hexdigest()[:16]


def _run_behavioral_scorers(
    trace_dict: dict[str, Any],
    trace_expectations: dict[str, Any],
) -> tuple[float, dict[str, Any]]:
    """Run deterministic trace scorers and return composite score + details.

    Runs: required_tools, banned_tools, tool_sequence.
    Returns (score 0-1, details dict).
    """
    scorers = [
        ("required_tools", required_tools_scorer),
        ("banned_tools", banned_tools_scorer),
        ("tool_sequence", tool_sequence_scorer),
    ]

    results: dict[str, Any] = {}
    passed = 0
    total = 0

    for name, scorer_fn in scorers:
        try:
            fb = scorer_fn(trace=trace_dict, expectations=trace_expectations)
            results[name] = {"value": fb.value, "rationale": fb.rationale}
            if fb.value == "yes":
                passed += 1
                total += 1
            elif fb.value == "no":
                total += 1
            # "skip" doesn't count toward total
        except Exception as e:
            results[name] = {"value": "error", "rationale": str(e)}

    score = passed / total if total > 0 else 0.5  # No expectations = neutral
    return score, results


def _compute_execution_success(agent_result: AgentResult) -> float:
    """Score based on whether tool calls succeeded.

    Returns ratio of successful tool calls (0-1).
    """
    tool_calls = agent_result.trace_metrics.tool_calls
    if not tool_calls:
        return 0.5  # No tool calls = neutral

    successful = sum(1 for tc in tool_calls if tc.success is True)
    total = sum(1 for tc in tool_calls if tc.success is not None)

    if total == 0:
        return 0.5

    return successful / total


class AgentEvaluator:
    """GEPA-compatible evaluator using real Claude Code agent + semantic grader.

    Runs a real Claude Code agent, captures the execution trace, and scores
    using a semantic assertion grader (hybrid deterministic + LLM) plus
    deterministic trace scorers for behavioral compliance.

    Args:
        original_token_counts: Token counts of original artifacts for efficiency scoring.
        token_budget: Hard token ceiling.
        judge_model: LLM model for semantic grading (from ``--judge-model``).
        mcp_config: MCP server configuration for the agent.
        allowed_tools: Allowed tools for the agent.
        agent_model: Model to use for the agent execution (from ``--agent-model``).
        agent_timeout: Timeout for each agent run in seconds.
    """

    def __init__(
        self,
        original_token_counts: dict[str, int] | None = None,
        token_budget: int | None = None,
        skill_guidelines: list[str] | None = None,
        judge_model: str | None = None,
        mcp_config: dict[str, Any] | None = None,
        allowed_tools: list[str] | None = None,
        agent_model: str | None = None,
        agent_timeout: int = 300,
        mlflow_experiment: str | None = None,
        skill_name: str | None = None,
        tool_modules: list[str] | None = None,
    ):
        self._original_token_counts = original_token_counts or {}
        self._total_original_tokens = sum(self._original_token_counts.values())
        self._token_budget = token_budget
        self._judge_model = judge_model
        self._mcp_config = mcp_config
        self._allowed_tools = allowed_tools
        self._agent_model = agent_model
        self._agent_timeout = agent_timeout
        self._mlflow_experiment = mlflow_experiment
        self._skill_name = skill_name
        self._tool_modules = tool_modules

        # Export resolved agent auth vars into os.environ so the semantic
        # grader (which runs in-process, not in the agent subprocess) can
        # read ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN and route through
        # the same Databricks AI Gateway endpoint as the agent.
        self._inject_grader_env()

        # Cache WITH-skill evaluation results keyed on (prompt_hash, candidate_hash)
        self._with_skill_cache: dict[str, tuple[float, dict]] = {}

        # Caches for WITHOUT-skill runs (keyed by prompt hash)
        self._baseline_response_cache: dict[str, str] = {}
        self._baseline_trace_cache: dict[str, dict] = {}
        self._cache_lock = threading.Lock()

    @staticmethod
    def _inject_grader_env() -> None:
        """Set agent auth vars in os.environ for the in-process grader.

        The semantic grader reads ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN
        from os.environ.  These are normally only set in the agent subprocess
        env (via _get_agent_env).  We load the same settings file here so
        the grader hits the same Databricks AI Gateway endpoint.
        """
        from ..agent.executor import _get_agent_env

        agent_env = _get_agent_env()
        _AUTH_KEYS = {
            "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
            "ANTHROPIC_MODEL", "ANTHROPIC_CUSTOM_HEADERS",
            "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        }
        for key in _AUTH_KEYS:
            if key in agent_env and key not in os.environ:
                os.environ[key] = agent_env[key]

    def _run_agent(self, prompt: str, skill_md: str | None = None) -> AgentResult:
        """Run the agent and return result. Synchronous wrapper."""
        return run_agent_sync_wrapper(
            prompt=prompt,
            skill_md=skill_md,
            mcp_config=self._mcp_config,
            allowed_tools=self._allowed_tools,
            timeout_seconds=self._agent_timeout,
            model=self._agent_model,
            mlflow_experiment=self._mlflow_experiment,
            skill_name=self._skill_name,
            tool_modules=self._tool_modules,
        )

    def _get_baseline(self, prompt: str) -> tuple[str, dict]:
        """Get WITHOUT-skill baseline response and trace dict, cached by prompt hash."""
        key = _prompt_hash(prompt)
        with self._cache_lock:
            if key in self._baseline_response_cache:
                return (
                    self._baseline_response_cache[key],
                    self._baseline_trace_cache[key],
                )
        # Agent run is expensive -- release lock while running
        result = self._run_agent(prompt, skill_md=None)
        with self._cache_lock:
            if key not in self._baseline_response_cache:
                self._baseline_response_cache[key] = result.response_text
                self._baseline_trace_cache[key] = result.trace_metrics.to_dict()
            return (
                self._baseline_response_cache[key],
                self._baseline_trace_cache[key],
            )

    def __call__(
        self,
        candidate: dict[str, str],
        example: dict,
    ) -> tuple[float, dict]:
        """Evaluate a candidate skill against a single task using agent execution.

        GEPA-compatible signature: (candidate, example) -> (score, side_info)

        Wrapped in try-except so that any uncaught exception (timeout, network
        error, etc.) returns a fallback zero score instead of crashing GEPA.
        """
        try:
            return self._evaluate(candidate, example)
        except Exception as e:
            logger.error("AgentEvaluator error for task: %s", e)
            return 0.0, {"_error": str(e), "scores": {"final": 0.0}}

    def _evaluate(
        self,
        candidate: dict[str, str],
        example: dict,
    ) -> tuple[float, dict]:
        """Inner evaluation logic, called by __call__ with error handling."""
        skill_md = candidate.get("skill_md", "")
        prompt = example.get("input", "")

        # Check candidate-level cache
        candidate_hash = hashlib.sha256(json.dumps(candidate, sort_keys=True).encode()).hexdigest()[:16]
        cache_key = f"{_prompt_hash(prompt)}:{candidate_hash}"
        if cache_key in self._with_skill_cache:
            return self._with_skill_cache[cache_key]

        # Decode expectations
        expectations: dict[str, Any] = {}
        expectations_json = example.get("additional_context", {}).get("expectations", "")
        if expectations_json:
            try:
                expectations = json.loads(expectations_json)
            except (json.JSONDecodeError, TypeError):
                pass

        trace_expectations = expectations.get("trace_expectations", {})

        if not prompt:
            return 0.0, {"_error": "No prompt for this task"}

        # Phase 1: Run agent WITH skill
        logger.info("Running agent WITH skill...")
        start = time.monotonic()
        with_result = self._run_agent(prompt, skill_md=skill_md)
        with_duration = time.monotonic() - start
        logger.info("WITH-skill agent completed in %.1fs", with_duration)

        # Phase 2: Run agent WITHOUT skill (cached)
        logger.info("Running agent WITHOUT skill (cached if available)...")
        without_response, without_trace = self._get_baseline(prompt)

        with_response = with_result.response_text
        with_trace = with_result.trace_metrics.to_dict()

        # Phase 3: Agent-based grading (Claude Code grader with transcript)
        with_transcript = [e.__dict__ if hasattr(e, '__dict__') else e for e in (with_result.events or [])]
        with_results, without_results, diagnostics = grade_with_without(
            with_response, without_response, expectations,
            judge_model=self._judge_model,
            with_transcript=with_transcript,
            agent_model=self._agent_model,
        )

        # Phase 4: Deterministic trace scorers (behavioral compliance)
        behavioral_score, behavioral_details = _run_behavioral_scorers(with_trace, trace_expectations)
        execution_success = _compute_execution_success(with_result)

        # Phase 5: Composite score (matches evaluate.py defaults)
        total_candidate_tokens = sum(count_tokens(v) for v in candidate.values())
        final_score, scores = compute_score(diagnostics)

        # Build side_info for GEPA reflection
        reference_answer = example.get("answer", "")
        side_info = build_side_info(
            prompt=prompt,
            with_results=with_results,
            without_results=without_results,
            diagnostics=diagnostics,
            with_response=with_response,
            without_response=without_response,
            reference_answer=reference_answer,
        )

        # Add agent-specific trace details
        side_info["agent_trace"] = {
            "total_tool_calls": with_trace.get("tools", {}).get("total_calls", 0),
            "tool_counts": with_trace.get("tools", {}).get("by_name", {}),
            "duration_ms": with_result.duration_ms,
            "success": with_result.success,
            "tokens": with_trace.get("tokens", {}),
        }
        side_info["behavioral_scores"] = behavioral_details
        side_info["execution_success"] = execution_success

        # Add scores and token counts
        side_info["scores"] = scores
        side_info["token_counts"] = {
            "candidate_total": total_candidate_tokens,
            "original_total": self._total_original_tokens,
        }
        if self._token_budget:
            side_info["token_counts"]["budget"] = self._token_budget

        # Store in candidate-level cache
        self._with_skill_cache[cache_key] = (final_score, side_info)

        return final_score, side_info


def _collect_skill_guidelines(skill_name: str) -> list[str]:
    """Collect and deduplicate guidelines from ground_truth.yaml and manifest.yaml."""
    from pathlib import Path
    import yaml

    seen: set[str] = set()
    guidelines: list[str] = []

    gt_path = Path(".test/skills") / skill_name / "ground_truth.yaml"
    if gt_path.exists():
        try:
            with open(gt_path) as f:
                data = yaml.safe_load(f) or {}
            for tc in data.get("test_cases", []):
                for g in tc.get("expectations", {}).get("guidelines", []):
                    g_norm = g.strip()
                    if g_norm and g_norm not in seen:
                        seen.add(g_norm)
                        guidelines.append(g_norm)
        except Exception:
            pass

    manifest_path = Path(".test/skills") / skill_name / "manifest.yaml"
    if manifest_path.exists():
        try:
            with open(manifest_path) as f:
                manifest = yaml.safe_load(f) or {}
            for g in manifest.get("scorers", {}).get("default_guidelines", []):
                g_norm = g.strip()
                if g_norm and g_norm not in seen:
                    seen.add(g_norm)
                    guidelines.append(g_norm)
        except Exception:
            pass

    return guidelines


def create_agent_evaluator(
    skill_name: str,
    original_token_counts: dict[str, int] | None = None,
    token_budget: int | None = None,
    judge_model: str | None = None,
    mcp_config: dict[str, Any] | None = None,
    allowed_tools: list[str] | None = None,
    agent_model: str | None = None,
    agent_timeout: int = 300,
    mlflow_experiment: str | None = None,
    tool_modules: list[str] | None = None,
) -> Callable:
    """Factory for agent-based evaluator with semantic grading.

    Returns a GEPA-compatible callable: (candidate, example) -> (score, side_info)

    Args:
        skill_name: Name of the skill being evaluated.
        judge_model: LLM model for semantic grading (from ``--judge-model``).
        agent_model: Model for Claude Code execution (from ``--agent-model``).
        tool_modules: MCP tool modules from manifest.yaml for criteria filtering.
    """
    skill_guidelines = _collect_skill_guidelines(skill_name)
    if skill_guidelines:
        logger.info("Loaded %d domain guidelines for semantic grader", len(skill_guidelines))

    return AgentEvaluator(
        original_token_counts=original_token_counts,
        token_budget=token_budget,
        skill_guidelines=skill_guidelines,
        judge_model=judge_model,
        mcp_config=mcp_config,
        allowed_tools=allowed_tools,
        agent_model=agent_model,
        agent_timeout=agent_timeout,
        mlflow_experiment=mlflow_experiment,
        skill_name=skill_name,
        tool_modules=tool_modules,
    )


def build_agent_eval_background(
    skill_name: str,
    original_token_count: int,
    baseline_scores: dict[str, float] | None = None,
    baseline_side_info: dict[str, dict] | None = None,
    focus_areas: list[str] | None = None,
) -> str:
    """Build GEPA reflection context specific to agent evaluation.

    Highlights focused judge signals and skill discovery.
    """
    baseline_desc = ""
    if baseline_scores:
        mean_score = sum(baseline_scores.values()) / len(baseline_scores)
        baseline_desc = f"\nBASELINE: mean {mean_score:.3f} across {len(baseline_scores)} tasks."

        if baseline_side_info:
            needs_skill_ids = []
            regression_ids = []
            tool_issues = []
            for tid, info in baseline_side_info.items():
                error = info.get("Error", "")
                if "NEEDS_SKILL" in error:
                    needs_skill_ids.append(tid)
                if "REGRESSION" in error:
                    regression_ids.append(tid)
                behavioral = info.get("behavioral_scores", {})
                for scorer_name, result in behavioral.items():
                    if result.get("value") == "no":
                        tool_issues.append(f"{tid}: {scorer_name} - {result.get('rationale', '')[:80]}")

            if needs_skill_ids:
                baseline_desc += f"\n  NEEDS_SKILL ({len(needs_skill_ids)} tasks): {', '.join(needs_skill_ids[:5])}"
            if regression_ids:
                baseline_desc += f"\n  REGRESSION ({len(regression_ids)} tasks): {', '.join(regression_ids[:5])}"
            if tool_issues:
                baseline_desc += f"\n  TOOL ISSUES ({len(tool_issues)}):"
                for issue in tool_issues[:5]:
                    baseline_desc += f"\n    - {issue}"

    focus_desc = ""
    if focus_areas:
        focus_items = "\n".join(f"  - {f}" for f in focus_areas)
        focus_desc = (
            f"\n\nUSER FOCUS PRIORITIES:\n{focus_items}\n"
            "These are high-priority areas the user wants the skill to emphasize. "
            "Weight these priorities heavily in your optimization decisions."
        )

    return (
        f"You are refining SKILL.md for '{skill_name}'.\n"
        "The skill is scored by a real Claude Code agent that executes tasks.\n"
        "A semantic assertion grader checks per-assertion pass/fail with evidence:\n"
        "  - Expected facts (substring + semantic matching)\n"
        "  - Expected patterns (regex matching)\n"
        "  - Guidelines and freeform assertions (semantic evaluation)\n\n"
        "Per-assertion classification tells you exactly what to fix:\n"
        "  NEEDS_SKILL -- fails both with and without (skill must teach this)\n"
        "  REGRESSION  -- passes without skill, fails with (skill is hurting)\n"
        "  POSITIVE    -- fails without, passes with (skill is helping)\n\n"
        "Deterministic trace scorers check tool usage and behavioral compliance.\n"
        "Failed_Assertions lists exactly WHAT is missing with evidence.\n\n"
        "Focus on: guiding the agent to use the RIGHT tools with CORRECT arguments.\n"
        "Avoid: unnecessary tool calls, wrong tool selection, verbose instructions."
        f"{baseline_desc}"
        f"{focus_desc}"
    )
