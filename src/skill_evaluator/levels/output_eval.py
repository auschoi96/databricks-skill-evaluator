"""Level 5: Output Eval (#408) — Agent output quality vs source of truth.

Evaluates WHAT the agent produces by comparing WITH-skill vs WITHOUT-skill
responses using the semantic grader. Supports source of truth comparison
and mandatory facts validation via LLM judge.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from .base import EvalLevel, LevelConfig, LevelResult

logger = logging.getLogger(__name__)

# Cache for WITHOUT-skill baselines (keyed by prompt hash)
_baseline_cache: dict[str, Any] = {}


class OutputEvalLevel(EvalLevel):
    """Evaluate agent output quality with WITH/WITHOUT comparison."""

    @property
    def name(self) -> str:
        return "output"

    @property
    def level_number(self) -> int:
        return 5

    @property
    def requires_agent(self) -> bool:
        return True

    @property
    def requires_workspace(self) -> bool:
        return True

    @property
    def requires_mcp(self) -> bool:
        return True

    def run(self, config: LevelConfig) -> LevelResult:
        from ..agent.executor import run_agent_sync_wrapper

        feedbacks: list[dict[str, Any]] = []
        task_results: list[dict[str, Any]] = []

        test_cases = config.test_instructions.ground_truth
        if not test_cases:
            return LevelResult(
                level=self.name, score=0.0,
                feedbacks=[{"name": "output/no_test_cases", "value": "skip",
                            "rationale": "No test cases in ground_truth.yaml", "source": "CODE"}],
            )

        all_scores = []

        for case in test_cases:
            prompt = case.inputs.get("prompt", "")
            case_id = case.id
            expectations = case.expectations or {}
            logger.info(f"Output eval: {case_id}")

            try:
                # Run agent WITH skill
                with_result = run_agent_sync_wrapper(
                    prompt=prompt,
                    skill_md=config.skill.skill_md_content,
                    mcp_config=config.mcp_config.servers if config.mcp_config else None,
                    timeout_seconds=config.agent_timeout,
                    model=config.agent_model,
                )

                # Run agent WITHOUT skill (cached by prompt hash)
                prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()[:12]
                if prompt_hash in _baseline_cache:
                    without_result = _baseline_cache[prompt_hash]
                    logger.info(f"  Using cached WITHOUT baseline for {case_id}")
                else:
                    without_result = run_agent_sync_wrapper(
                        prompt=prompt,
                        skill_md=None,
                        mcp_config=config.mcp_config.servers if config.mcp_config else None,
                        timeout_seconds=config.agent_timeout,
                        model=config.agent_model,
                    )
                    _baseline_cache[prompt_hash] = without_result

                # Semantic grading
                try:
                    from ..grading.semantic_grader import grade_with_without, compute_score

                    with_transcript = None
                    if hasattr(with_result, "events") and with_result.events:
                        with_transcript = [
                            {"type": e.type, "data": str(e.data)[:500]}
                            for e in with_result.events[:50]
                        ]

                    with_assertions, without_assertions, diagnostics = grade_with_without(
                        with_response=with_result.response_text,
                        without_response=without_result.response_text,
                        expectations=expectations,
                        judge_model=config.judge_model or "databricks/databricks-claude-sonnet-4-6",
                        with_transcript=with_transcript,
                    )

                    final_score, score_breakdown = compute_score(diagnostics)
                    all_scores.append(final_score)

                    # Convert assertion results to feedbacks
                    for assertion in with_assertions:
                        classification = assertion.get("classification", "NEUTRAL")
                        feedbacks.append({
                            "name": f"output/{case_id}/{assertion.get('text', 'unknown')[:50]}",
                            "value": "pass" if assertion.get("passed") else "fail",
                            "rationale": f"[{classification}] {assertion.get('evidence', '')}",
                            "source": "LLM_JUDGE",
                        })

                    task_results.append({
                        "task_id": case_id,
                        "prompt": prompt,
                        "with_response": with_result.response_text[:500],
                        "without_response": without_result.response_text[:500],
                        "scores": score_breakdown,
                        "final_score": final_score,
                        "assertions": with_assertions,
                        "diagnostics": diagnostics,
                    })

                except ImportError:
                    # Fallback: simple assertion checking without semantic grader
                    task_feedbacks = self._simple_assertion_check(
                        case_id, with_result.response_text, expectations
                    )
                    feedbacks.extend(task_feedbacks)
                    passed = sum(1 for f in task_feedbacks if f["value"] == "pass")
                    total = len(task_feedbacks)
                    simple_score = passed / total if total > 0 else 0.0
                    all_scores.append(simple_score)

            except Exception as e:
                logger.error(f"Agent execution failed for {case_id}: {e}")
                feedbacks.append({
                    "name": f"output/{case_id}/execution",
                    "value": "fail",
                    "rationale": f"Agent execution failed: {e}",
                    "source": "CODE",
                })
                all_scores.append(0.0)

        # Compute overall score
        score = sum(all_scores) / len(all_scores) if all_scores else 0.0

        return LevelResult(
            level=self.name,
            score=score,
            feedbacks=feedbacks,
            task_results=task_results,
            metadata={
                "pass_rate_with": score,
                "num_test_cases": len(test_cases),
                "num_assertions": len(feedbacks),
            },
        )

    def _simple_assertion_check(
        self, case_id: str, response: str, expectations: dict
    ) -> list[dict[str, Any]]:
        """Fallback assertion checking without the semantic grader."""
        feedbacks = []
        response_lower = response.lower()

        # Check expected facts
        for fact in expectations.get("expected_facts", []):
            found = fact.lower() in response_lower
            feedbacks.append({
                "name": f"output/{case_id}/fact/{fact[:40]}",
                "value": "pass" if found else "fail",
                "rationale": f"Fact '{fact}' {'found' if found else 'NOT found'} in response",
                "source": "CODE",
            })

        # Check expected patterns
        import re
        for pat_config in expectations.get("expected_patterns", []):
            pattern = pat_config if isinstance(pat_config, str) else pat_config.get("pattern", "")
            min_count = pat_config.get("min_count", 1) if isinstance(pat_config, dict) else 1
            matches = len(re.findall(pattern, response, re.IGNORECASE))
            passed = matches >= min_count
            feedbacks.append({
                "name": f"output/{case_id}/pattern/{pattern[:40]}",
                "value": "pass" if passed else "fail",
                "rationale": f"Pattern '{pattern}': {matches} matches (need >={min_count})",
                "source": "CODE",
            })

        return feedbacks
