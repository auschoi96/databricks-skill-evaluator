"""Level 4: Thinking Eval (#407) — Agent reasoning quality assessment.

Evaluates HOW the agent reasons during execution: efficiency, clarity,
recovery, completeness. Uses custom per-skill thinking_instructions.md
and deterministic trace scorers.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from .base import EvalLevel, LevelConfig, LevelResult

logger = logging.getLogger(__name__)

_THINKING_JUDGE_PROMPT = """You are evaluating how well a Claude Code agent reasoned through a task. Focus on the PROCESS, not the output.

## Task
{prompt}

## Custom Evaluation Criteria
{thinking_instructions}

## Agent Execution Transcript
{transcript}

## Trace Summary
- Total tool calls: {total_tool_calls}
- Tool breakdown: {tool_counts}
- Total tokens: {total_tokens}
- Turns: {num_turns}
- Errors encountered: {errors}

## Evaluation Dimensions

Score each 1-5 with specific evidence from the transcript:

1. **EFFICIENCY**: Did the agent use the minimum necessary tool calls? Did it avoid redundant reads, unnecessary retries, or roundabout approaches?

2. **CLARITY**: Did the agent's reasoning show clear understanding of the task? Or did it show confusion, backtracking, or misunderstanding?

3. **RECOVERY**: When errors occurred, did the agent recover gracefully? Did it diagnose the issue and try a reasonable alternative?

4. **COMPLETENESS**: Did the agent complete all required steps? Were any critical actions skipped or left incomplete?

Return JSON:
```json
[
  {{"dimension": "efficiency", "score": 4, "evidence": "Agent used 2 tool calls for a simple creation task"}},
  {{"dimension": "clarity", "score": 5, "evidence": "Clear reasoning throughout, no backtracking"}},
  {{"dimension": "recovery", "score": 3, "evidence": "Recovered from missing table error but took 2 extra calls"}},
  {{"dimension": "completeness", "score": 5, "evidence": "All required steps completed successfully"}}
]
```"""


class ThinkingEvalLevel(EvalLevel):
    """Evaluate agent reasoning quality from execution traces."""

    @property
    def name(self) -> str:
        return "thinking"

    @property
    def level_number(self) -> int:
        return 4

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
                feedbacks=[{"name": "thinking/no_test_cases", "value": "skip",
                            "rationale": "No test cases in ground_truth.yaml", "source": "CODE"}],
            )

        thinking_instructions = (
            config.test_instructions.thinking_instructions
            or "No custom thinking instructions provided. Evaluate general reasoning quality."
        )

        for case in test_cases:
            prompt = case.inputs.get("prompt", "")
            case_id = case.id
            logger.info(f"Thinking eval: {case_id}")

            try:
                # Run agent WITH skill
                result = run_agent_sync_wrapper(
                    prompt=prompt,
                    skill_md=config.skill.skill_md_content,
                    mcp_config=config.mcp_config.servers if config.mcp_config else None,
                    timeout_seconds=config.agent_timeout,
                    model=config.agent_model,
                )

                # Deterministic trace scoring
                trace_feedbacks = self._score_trace(case, result)
                feedbacks.extend(trace_feedbacks)

                # LLM judge for reasoning quality
                llm_feedbacks, dim_scores = self._judge_thinking(
                    prompt=prompt,
                    result=result,
                    thinking_instructions=thinking_instructions,
                    judge_model=config.judge_model,
                )
                feedbacks.extend(llm_feedbacks)

                task_results.append({
                    "task_id": case_id,
                    "prompt": prompt,
                    "dimension_scores": dim_scores,
                    "trace_summary": {
                        "tool_calls": result.trace_metrics.total_tool_calls if result.trace_metrics else 0,
                        "tokens": result.trace_metrics.total_tokens if result.trace_metrics else 0,
                    },
                })

            except Exception as e:
                logger.error(f"Agent execution failed for {case_id}: {e}")
                feedbacks.append({
                    "name": f"thinking/{case_id}/execution",
                    "value": "fail",
                    "rationale": f"Agent execution failed: {e}",
                    "source": "CODE",
                })

        # Compute overall score
        all_scores = [
            f for f in feedbacks
            if f.get("source") == "LLM_JUDGE" and f["value"] != "skip"
        ]
        if all_scores:
            pass_rate = sum(1 for f in all_scores if f["value"] == "pass") / len(all_scores)
            score = pass_rate
        else:
            score = 0.0

        return LevelResult(
            level=self.name,
            score=score,
            feedbacks=feedbacks,
            task_results=task_results,
        )

    def _score_trace(self, case, result) -> list[dict[str, Any]]:
        """Run deterministic trace scorers."""
        feedbacks = []
        trace = result.trace_metrics
        if not trace:
            return feedbacks

        expectations = (case.expectations or {}).get("trace_expectations", {})

        # Required tools check
        required = expectations.get("required_tools", [])
        for tool in required:
            found = trace.has_tool(tool)
            feedbacks.append({
                "name": f"thinking/{case.id}/required_tool/{tool}",
                "value": "pass" if found else "fail",
                "rationale": f"Required tool '{tool}' {'used' if found else 'NOT used'}",
                "source": "CODE",
            })

        # Banned tools check
        banned = expectations.get("banned_tools", [])
        for tool in banned:
            used = trace.has_tool(tool)
            feedbacks.append({
                "name": f"thinking/{case.id}/banned_tool/{tool}",
                "value": "pass" if not used else "fail",
                "rationale": f"Banned tool '{tool}' {'NOT used (good)' if not used else 'was USED'}",
                "source": "CODE",
            })

        # Tool limits
        limits = expectations.get("tool_limits", {})
        for tool_name, max_count in limits.items():
            actual = trace.get_tool_count(tool_name)
            within = actual <= max_count
            feedbacks.append({
                "name": f"thinking/{case.id}/tool_limit/{tool_name}",
                "value": "pass" if within else "fail",
                "rationale": f"Tool '{tool_name}': {actual} calls (limit: {max_count})",
                "source": "CODE",
            })

        # Token budget
        token_budget = expectations.get("token_budget", {})
        max_total = token_budget.get("max_total")
        if max_total:
            actual = trace.total_tokens
            within = actual <= max_total
            feedbacks.append({
                "name": f"thinking/{case.id}/token_budget",
                "value": "pass" if within else "fail",
                "rationale": f"Total tokens: {actual} (budget: {max_total})",
                "source": "CODE",
            })

        return feedbacks

    def _judge_thinking(
        self, prompt: str, result, thinking_instructions: str, judge_model: str | None
    ) -> tuple[list[dict], dict[str, float]]:
        """LLM judge for reasoning quality dimensions."""
        try:
            from ..grading.llm_backend import completion_with_fallback
        except ImportError:
            return [], {}

        trace = result.trace_metrics
        # Build transcript text from events
        transcript_text = ""
        if hasattr(result, "events") and result.events:
            for event in result.events[:50]:  # Limit to 50 events
                transcript_text += f"[{event.type}] {str(event.data)[:500]}\n"
        elif hasattr(result, "response_text"):
            transcript_text = result.response_text[:3000]

        # Detect errors in transcript
        errors = []
        if trace:
            for tc in trace.tool_calls:
                if tc.success is False:
                    errors.append(f"{tc.name}: failed")

        judge_prompt = _THINKING_JUDGE_PROMPT.format(
            prompt=prompt,
            thinking_instructions=thinking_instructions,
            transcript=transcript_text[:5000],
            total_tool_calls=trace.total_tool_calls if trace else "unknown",
            tool_counts=json.dumps(trace.tool_counts if trace else {}),
            total_tokens=trace.total_tokens if trace else "unknown",
            num_turns=trace.num_turns if trace else "unknown",
            errors=", ".join(errors) if errors else "none",
        )

        model = judge_model or "databricks/databricks-claude-sonnet-4-6"

        try:
            response = completion_with_fallback(
                model=model,
                messages=[{"role": "user", "content": judge_prompt}],
                temperature=0,
            )
            content = response.choices[0].message.content
            json_match = re.search(r"\[.*\]", content, re.DOTALL)
            if not json_match:
                return [], {}

            dimensions = json.loads(json_match.group())
        except Exception as e:
            logger.error(f"Thinking judge failed: {e}")
            return [], {}

        feedbacks = []
        scores = {}
        for dim in dimensions:
            dim_id = dim.get("dimension", "unknown")
            dim_score = dim.get("score", 3)
            evidence = dim.get("evidence", "")

            scores[dim_id] = float(dim_score)
            feedbacks.append({
                "name": f"thinking/{dim_id}",
                "value": "pass" if dim_score >= 3 else "fail",
                "rationale": f"Score: {dim_score}/5. {evidence}",
                "source": "LLM_JUDGE",
            })

        return feedbacks, scores
