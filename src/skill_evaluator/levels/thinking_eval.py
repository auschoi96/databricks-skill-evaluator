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

# Maximum characters for the transcript in the judge prompt.
# A typical agent session is 5-20 turns; 15K chars is enough for
# comprehensive tool_use/result pairs plus reasoning text while
# staying well within the model context window.
_TRANSCRIPT_BUDGET = 15_000

# Per-field truncation limits for individual event data.
_TOOL_INPUT_LIMIT = 1000
_TOOL_RESULT_LIMIT = 1000
_TEXT_BLOCK_LIMIT = 2000


def _build_comprehensive_transcript(events: list, budget: int = _TRANSCRIPT_BUDGET) -> str:
    """Build a structured, human-readable transcript from agent events.

    Groups events by turn and formats them so the LLM judge can assess:
    - THINKING: agent reasoning text (critical for clarity scoring)
    - TOOL_USE → TOOL_RESULT pairs (critical for efficiency/recovery scoring)
    - ERROR markers (critical for recovery scoring)
    - Per-turn token usage (supports efficiency scoring)
    """
    if not events:
        return "(no events captured)"

    lines: list[str] = []
    turn_num = 0
    # Map tool_use IDs to their names for pairing with results
    pending_tools: dict[str, str] = {}

    for event in events:
        etype = getattr(event, "type", "") if hasattr(event, "type") else str(event)
        data = getattr(event, "data", {}) if hasattr(event, "data") else {}

        if etype == "assistant_turn":
            turn_num += 1
            usage = data.get("usage", {})
            inp = usage.get("input_tokens", 0)
            out = usage.get("output_tokens", 0)
            lines.append(f"\n{'=' * 40}")
            lines.append(f"TURN {turn_num}  (tokens: input={inp} output={out})")
            lines.append("=" * 40)

        elif etype == "text":
            text = data.get("text", "")
            if text.strip():
                truncated = text[:_TEXT_BLOCK_LIMIT]
                if len(text) > _TEXT_BLOCK_LIMIT:
                    truncated += f"... [{len(text) - _TEXT_BLOCK_LIMIT} chars truncated]"
                lines.append(f"\n[THINKING] {truncated}")

        elif etype == "tool_use":
            tool_name = data.get("name", "unknown_tool")
            tool_id = data.get("id", "")
            tool_input = data.get("input", {})
            pending_tools[tool_id] = tool_name

            input_str = json.dumps(tool_input, default=str)
            if len(input_str) > _TOOL_INPUT_LIMIT:
                input_str = input_str[:_TOOL_INPUT_LIMIT] + "..."
            lines.append(f"\n[TOOL_USE] {tool_name}")
            lines.append(f"  Input: {input_str}")

        elif etype == "tool_result":
            tool_id = data.get("tool_use_id", "")
            is_error = data.get("is_error", False)
            content = data.get("content", "")

            tool_name = pending_tools.pop(tool_id, "unknown_tool")
            status = "ERROR" if is_error else "SUCCESS"

            # Extract meaningful content from tool result
            result_str = str(content) if content else "(empty)"
            if len(result_str) > _TOOL_RESULT_LIMIT:
                result_str = result_str[:_TOOL_RESULT_LIMIT] + "..."

            prefix = "[ERROR]" if is_error else "[TOOL_RESULT]"
            lines.append(f"{prefix} {status} ({tool_name})")
            lines.append(f"  {result_str}")

        elif etype == "error":
            msg = data.get("message", str(data))
            lines.append(f"\n[ERROR] {msg}")

        elif etype == "system":
            subtype = data.get("subtype", "")
            if subtype:
                lines.append(f"\n[SYSTEM] {subtype}")

    transcript = "\n".join(lines)

    # Trim to budget, preserving complete lines
    if len(transcript) > budget:
        # Keep the beginning (shows initial approach) and end (shows completion)
        half = budget // 2
        head = transcript[:half]
        tail = transcript[-half:]
        # Cut at line boundaries
        head = head[:head.rfind("\n")] if "\n" in head else head
        tail = tail[tail.find("\n") + 1:] if "\n" in tail else tail
        transcript = head + f"\n\n... [{len(transcript) - budget} chars omitted] ...\n\n" + tail

    return transcript


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
        trace_ids: list[str] = []

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

                # Capture MLflow trace ID for assessment logging
                if result.mlflow_trace_id:
                    trace_ids.append(result.mlflow_trace_id)

                task_results.append({
                    "task_id": case_id,
                    "prompt": prompt,
                    "dimension_scores": dim_scores,
                    "trace_summary": {
                        "tool_calls": result.trace_metrics.total_tool_calls if result.trace_metrics else 0,
                        "tokens": result.trace_metrics.total_tokens if result.trace_metrics else 0,
                    },
                    "mlflow_trace_id": result.mlflow_trace_id,
                })

            except Exception as e:
                logger.error(f"Agent execution failed for {case_id}: {e}")
                feedbacks.append({
                    "name": f"thinking/{case_id}/execution",
                    "value": "fail",
                    "rationale": f"Agent execution failed: {e}",
                    "source": "CODE",
                })

        # Compute overall score from actual dimension scores (1-5 scale),
        # normalized to 0-1.  This preserves granularity — a test scoring
        # 5/5/5/5 is meaningfully different from 3/3/3/3.
        all_dim_scores: list[float] = []
        for tr in task_results:
            for dim_score in tr.get("dimension_scores", {}).values():
                all_dim_scores.append(float(dim_score))

        if all_dim_scores:
            score = sum(all_dim_scores) / (len(all_dim_scores) * 5)
        else:
            score = 0.0

        return LevelResult(
            level=self.name,
            score=score,
            feedbacks=feedbacks,
            task_results=task_results,
            trace_ids=trace_ids,
        )

    def _score_trace(self, case, result) -> list[dict[str, Any]]:
        """Run deterministic trace scorers."""
        from .shared_validators import check_trace_expectations

        trace = result.trace_metrics
        if not trace:
            return []

        # Shared checks: required_tools, banned_tools, tool_limits
        feedbacks = check_trace_expectations(
            case_id=case.id,
            trace=trace,
            expectations=case.expectations or {},
            level_prefix="thinking",
        )

        # L4-only: token budget check
        trace_exp = (case.expectations or {}).get("trace_expectations", {})
        token_budget = trace_exp.get("token_budget", {})
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

        # Build comprehensive transcript from events (primary path).
        # Includes agent reasoning text, structured tool_use→result pairs,
        # error markers, and per-turn token usage.
        if hasattr(result, "events") and result.events:
            transcript_text = _build_comprehensive_transcript(result.events)
        elif hasattr(result, "response_text"):
            transcript_text = result.response_text[:_TRANSCRIPT_BUDGET]
        else:
            transcript_text = "(no transcript available)"

        # Detect errors in trace for the summary section
        errors = []
        if trace:
            for tc in trace.tool_calls:
                if tc.success is False:
                    errors.append(f"{tc.name}: failed")

        judge_prompt = _THINKING_JUDGE_PROMPT.format(
            prompt=prompt,
            thinking_instructions=thinking_instructions,
            transcript=transcript_text,
            total_tool_calls=trace.total_tool_calls if trace else "unknown",
            tool_counts=json.dumps(trace.tool_counts if trace else {}),
            total_tokens=trace.total_tokens if trace else "unknown",
            num_turns=trace.num_turns if trace else "unknown",
            errors=", ".join(errors) if errors else "none",
        )

        model = judge_model or "databricks/databricks-claude-opus-4-6"

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
