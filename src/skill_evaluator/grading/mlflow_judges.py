"""MLflow make_judge() wrappers for LLM-based evaluation judges.

Follows the MLflow LLM-as-a-judge pattern (mlflow.org/blog/evaluating-skills-mlflow).
Each function creates a make_judge() instance for a specific evaluation context
(static eval, thinking eval, asset verification, source of truth comparison).

All functions gracefully return None when make_judge is unavailable, allowing
the calling level to fall back to the existing litellm path.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def create_static_eval_judge(
    skill_content: str,
    available_tools: list[str] | None = None,
    model: str | None = None,
):
    """Create make_judge for L3 static evaluation (10 quality dimensions).

    Returns a callable judge or None if make_judge is unavailable.
    """
    try:
        from mlflow.genai.judges import make_judge
        from typing import Literal

        tools_context = ""
        if available_tools:
            tools_context = f"\nAvailable MCP tools: {', '.join(available_tools[:50])}"

        return make_judge(
            name="static-eval-quality",
            instructions=f"""You are evaluating the quality of a Claude Code SKILL.md document.
Score each of the following dimensions 1-10 with specific evidence (quotes from the skill):

1. **Self-Contained**: Can an agent follow this skill without external context?
2. **No Conflicting Information**: Are there any contradictory instructions?
3. **Security**: Does the skill avoid hardcoded secrets or unsafe patterns?
4. **LLM-Navigable Structure**: Is the document well-organized with clear headings?
5. **Actionable Instructions**: Are the instructions concrete and executable?
6. **Scoped Clearly**: Does the skill define what it does and doesn't cover?
7. **Tools/CLI Accuracy**: Do referenced tools and CLI commands exist?
8. **Examples Are Valid**: Do code examples have correct syntax?
9. **Error Handling Guidance**: Does the skill explain what to do when things fail?
10. **No Hallucination Triggers**: Does the skill avoid patterns that cause hallucination?
{tools_context}

Return a JSON array with one object per dimension:
[{{"dimension": "self_contained", "score": 8, "evidence": "...", "recommendation": "..."}}]
""",
            feedback_value_type=Literal["yes", "no"],
            model=model or "databricks/databricks-claude-opus-4-6",
        )
    except (ImportError, Exception) as e:
        logger.debug(f"make_judge unavailable for static eval: {e}")
        return None


def create_thinking_judge(
    thinking_instructions: str,
    model: str | None = None,
):
    """Create make_judge for L4 thinking evaluation (4 reasoning dimensions).

    Returns a callable judge or None if make_judge is unavailable.
    """
    try:
        from mlflow.genai.judges import make_judge
        from typing import Literal

        return make_judge(
            name="thinking-quality",
            instructions=f"""You are evaluating how well a Claude Code agent reasoned through a task.
Focus on the PROCESS, not the output.

## Custom Evaluation Criteria
{thinking_instructions}

Score each dimension 1-5 with specific evidence from the transcript:

1. **EFFICIENCY**: Did the agent use the minimum necessary tool calls?
2. **CLARITY**: Did the agent's reasoning show clear understanding?
3. **RECOVERY**: When errors occurred, did the agent recover gracefully?
4. **COMPLETENESS**: Did the agent complete all required steps?

Return JSON array:
[{{"dimension": "efficiency", "score": 4, "evidence": "..."}}]
""",
            feedback_value_type=Literal["yes", "no"],
            model=model or "databricks/databricks-claude-opus-4-6",
        )
    except (ImportError, Exception) as e:
        logger.debug(f"make_judge unavailable for thinking eval: {e}")
        return None


def create_asset_judge(
    assertions: list[str],
    model: str | None = None,
):
    """Create make_judge for L5 asset verification assertions.

    Returns a callable judge or None if make_judge is unavailable.
    """
    try:
        from mlflow.genai.judges import make_judge
        from typing import Literal

        assertions_block = "\n".join(f"{i}. {a}" for i, a in enumerate(assertions))

        return make_judge(
            name="asset-verification",
            instructions=f"""Verify whether these assertions are satisfied based on the agent's
tool calls and their results.

## Assertions to Verify
{assertions_block}

For each assertion, determine if it PASSED or FAILED based on the tool calls.
Return JSON array:
[{{"index": 0, "passed": true, "evidence": "Tool call created space with 3 tables"}}]
""",
            feedback_value_type=Literal["yes", "no"],
            model=model or "databricks/databricks-claude-opus-4-6",
        )
    except (ImportError, Exception) as e:
        logger.debug(f"make_judge unavailable for asset verification: {e}")
        return None


def create_sot_judge(model: str | None = None):
    """Create make_judge for L5 source-of-truth comparison.

    Returns a callable judge or None if make_judge is unavailable.
    """
    try:
        from mlflow.genai.judges import make_judge
        from typing import Literal

        return make_judge(
            name="source-of-truth-comparison",
            instructions="""Compare the actual agent output against the expected source of truth.

Rate the match on these dimensions:
1. **Structural match**: Does the actual output have the same structure/components?
2. **Content accuracy**: Are the key values/data correct?
3. **Completeness**: Is everything from the expected output present?

Return JSON:
[
  {"dimension": "structural_match", "score": 8, "evidence": "..."},
  {"dimension": "content_accuracy", "score": 7, "evidence": "..."},
  {"dimension": "completeness", "score": 9, "evidence": "..."}
]
""",
            feedback_value_type=Literal["yes", "no"],
            model=model or "databricks/databricks-claude-opus-4-6",
        )
    except (ImportError, Exception) as e:
        logger.debug(f"make_judge unavailable for SoT comparison: {e}")
        return None
