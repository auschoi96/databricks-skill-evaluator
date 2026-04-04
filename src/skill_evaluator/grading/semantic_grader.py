"""Semantic assertion grader: hybrid deterministic + LLM-based evaluation.

Extracted from ai-dev-kit/.test/src/skill_test/optimize/semantic_grader.py

Replaces the separate judges.py scoring and assertions.py modules with a
unified grading approach:

  1. Check expected_patterns deterministically (regex — zero cost)
  2. Check expected_facts deterministically (substring — zero cost)
  3. For deterministic failures + freeform assertions + guidelines:
     batch into 1 LLM call for semantic evaluation

Returns per-assertion pass/fail with evidence, which provides more granular
signal to GEPA than binary judges (5 assertions = 6 score levels vs 2).

The grade_with_without() function is the GEPA-compatible evaluator:
  (with_response, without_response, expectations) -> (score, side_info)
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class AssertionResult:
    """Result of a single assertion check."""

    text: str  # The assertion being checked
    passed: bool  # Binary pass/fail
    evidence: str  # Quote or explanation from the response
    method: str  # "deterministic" | "semantic"
    assertion_type: str = "assertion"  # "pattern" | "fact" | "assertion" | "guideline"


# ---------------------------------------------------------------------------
# Deterministic checks (zero LLM cost)
# ---------------------------------------------------------------------------


def _check_patterns(response: str, expected_patterns: list) -> list[AssertionResult]:
    """Check regex patterns deterministically. Zero LLM cost."""
    results = []
    for pattern_spec in expected_patterns:
        if isinstance(pattern_spec, str):
            pattern = pattern_spec
            min_count = 1
            max_count = None
            description = pattern[:60]
        else:
            pattern = pattern_spec["pattern"]
            min_count = pattern_spec.get("min_count", 1)
            max_count = pattern_spec.get("max_count", None)
            description = pattern_spec.get("description", pattern[:60])

        matches = len(re.findall(pattern, response, re.IGNORECASE))

        if max_count is not None:
            passed = min_count <= matches <= max_count
            evidence = f"Found {matches} matches (need {min_count}-{max_count})"
        else:
            passed = matches >= min_count
            evidence = f"Found {matches} matches (need >={min_count})"

        results.append(
            AssertionResult(
                text=description,
                passed=passed,
                evidence=evidence,
                method="deterministic",
                assertion_type="pattern",
            )
        )
    return results


def _check_facts(response: str, expected_facts: list[str]) -> list[AssertionResult]:
    """Check facts via case-insensitive substring match. Zero LLM cost."""
    response_lower = response.lower()
    results = []
    for fact in expected_facts:
        found = fact.lower() in response_lower
        results.append(
            AssertionResult(
                text=fact,
                passed=found,
                evidence=f"{'Found' if found else 'Missing'}: {fact}",
                method="deterministic",
                assertion_type="fact",
            )
        )
    return results


# ---------------------------------------------------------------------------
# Semantic grading (1 LLM call for all items that need it)
# ---------------------------------------------------------------------------

_SEMANTIC_GRADER_PROMPT = """\
You are an assertion grader. For each assertion below, determine whether the \
response satisfies it.

## Response to evaluate
{response}

## Assertions to check
{assertions_block}

## Instructions
For each assertion, return a JSON object with:
- "index": the assertion number (starting from 0)
- "passed": true or false
- "evidence": a brief quote or explanation from the response (max 100 chars)

Return ONLY a JSON array of objects. No markdown, no explanation.

Example:
[{{"index": 0, "passed": true, "evidence": "Response says 'use CREATE OR REPLACE VIEW'"}},
 {{"index": 1, "passed": false, "evidence": "No mention of MEASURE() function"}}]
"""


def _semantic_grade(
    response: str,
    assertions: list[str],
    judge_model: str | None = None,
) -> list[AssertionResult]:
    """Grade assertions semantically via a single LLM call.

    Args:
        response: The text to evaluate assertions against.
        assertions: List of freeform assertion strings.
        judge_model: Model to use for grading.

    Returns:
        List of AssertionResult with semantic evidence.
    """
    if not assertions:
        return []

    from ..grading.llm_backend import completion_with_fallback

    assertions_block = "\n".join(f"{i}. {a}" for i, a in enumerate(assertions))

    prompt = _SEMANTIC_GRADER_PROMPT.format(
        response=response,
        assertions_block=assertions_block,
    )

    model = judge_model or "databricks/databricks-claude-opus-4-6"

    try:
        resp = completion_with_fallback(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )
        raw = resp.choices[0].message.content or "[]"

        # Strip markdown fences if present
        raw = raw.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)

        grades = json.loads(raw)
    except Exception as e:
        logger.warning("Semantic grading LLM call failed: %s — marking all as failed", e)
        return [
            AssertionResult(
                text=a,
                passed=False,
                evidence=f"Semantic grading failed: {e}",
                method="semantic",
                assertion_type="assertion",
            )
            for a in assertions
        ]

    # Map LLM response back to assertions
    grade_map = {g["index"]: g for g in grades if isinstance(g, dict)}

    results = []
    for i, assertion_text in enumerate(assertions):
        grade = grade_map.get(i, {})
        results.append(
            AssertionResult(
                text=assertion_text,
                passed=bool(grade.get("passed", False)),
                evidence=str(grade.get("evidence", "No evidence returned")),
                method="semantic",
                assertion_type="assertion",
            )
        )

    return results


# ---------------------------------------------------------------------------
# Agent-based grading (Claude Code grader — Anthropic skill-creator pattern)
# ---------------------------------------------------------------------------

_AGENT_GRADER_PROMPT = """\
Grade each assertion below against the agent's response and transcript.

## Agent Response
{response}

## Execution Transcript
{transcript}

## Assertions to Evaluate
{assertions_block}

## Task

For each assertion, determine PASS or FAIL based on evidence in the response \
and transcript. Cite specific evidence.

Write your answer as a JSON code block:

```json
{{
  "assertions": [
    {{"index": 0, "passed": true, "evidence": "Response says 'use CREATE OR REPLACE VIEW'"}},
    {{"index": 1, "passed": false, "evidence": "No mention of MEASURE() function"}}
  ]
}}
```

IMPORTANT: Include the ```json code fence. Every assertion must have index, passed, and evidence.
"""


def _format_transcript(transcript: list[dict] | None) -> str:
    """Format agent execution transcript for the grader."""
    if not transcript:
        return "(No transcript available)"
    lines = []
    for event in transcript:
        etype = event.get("type", "unknown")
        data = event.get("data", {})
        if etype == "tool_use":
            lines.append(f"[TOOL_USE] {data.get('name', '?')}: {str(data.get('input', ''))}")
        elif etype == "tool_result":
            content = str(data.get("content", ""))
            lines.append(f"[TOOL_RESULT] {content}")
        elif etype == "text":
            lines.append(f"[TEXT] {str(data.get('text', data.get('content', '')))}")
    return "\n".join(lines) if lines else "(Empty transcript)"


def _agent_grade(
    response: str,
    assertions: list[str],
    transcript: list[dict] | None = None,
    judge_model: str | None = None,
    agent_timeout: int = 90,
) -> list[AssertionResult]:
    """Grade assertions using Databricks FMAPI with transcript context.

    Uses the transcript-aware prompt so the judge can see which MCP tools
    the agent called, not just the final response text.  Routes through
    ``completion_with_fallback`` (Databricks serving endpoints) for
    consistent auth and automatic model fallback on rate limits.

    Args:
        response: The agent's final response text.
        assertions: List of assertion strings to evaluate.
        transcript: Serialized agent events (tool_use, tool_result, text).
        judge_model: Databricks model for grading (e.g. "databricks/databricks-claude-opus-4-6").
        agent_timeout: Unused, kept for API compat.
    """
    if not assertions:
        return []

    from ..grading.llm_backend import completion_with_fallback

    assertions_block = "\n".join(f"{i}. {a}" for i, a in enumerate(assertions))
    transcript_text = _format_transcript(transcript)

    prompt = _AGENT_GRADER_PROMPT.format(
        response=response,
        transcript=transcript_text,
        assertions_block=assertions_block,
    )

    model = judge_model or "databricks/databricks-claude-opus-4-6"

    try:
        resp = completion_with_fallback(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )

        raw = resp.choices[0].message.content or ""
        raw = raw.strip()
        if not raw:
            logger.warning("Agent grader returned empty response")
            return [
                AssertionResult(
                    text=a, passed=False, evidence="Grader returned empty response",
                    method="agent", assertion_type="assertion",
                )
                for a in assertions
            ]

        # Strip markdown fences
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)

        # Extract JSON from response (model may include preamble)
        json_match = re.search(r'\{[\s\S]*\}|\[[\s\S]*\]', raw)
        if json_match:
            raw = json_match.group(0)

        parsed = json.loads(raw)
        grades = parsed.get("assertions", parsed) if isinstance(parsed, dict) else parsed

    except Exception as e:
        logger.warning("Agent grading failed: %s — marking all as failed", e)
        return [
            AssertionResult(
                text=a, passed=False, evidence=f"Agent grading failed: {e}",
                method="agent", assertion_type="assertion",
            )
            for a in assertions
        ]

    # Map response back to assertions
    if isinstance(grades, list):
        grade_map = {g["index"]: g for g in grades if isinstance(g, dict) and "index" in g}
    else:
        grade_map = {}

    results = []
    for i, assertion_text in enumerate(assertions):
        grade = grade_map.get(i, {})
        results.append(
            AssertionResult(
                text=assertion_text,
                passed=bool(grade.get("passed", False)),
                evidence=str(grade.get("evidence", "No evidence returned")),
                method="agent",
                assertion_type="assertion",
            )
        )

    return results


# ---------------------------------------------------------------------------
# Main grading function
# ---------------------------------------------------------------------------


def grade_assertions(
    response: str,
    expected_facts: list[str] | None = None,
    expected_patterns: list | None = None,
    guidelines: list[str] | None = None,
    assertions: list[str] | None = None,
    judge_model: str | None = None,
    transcript: list[dict] | None = None,
) -> list[AssertionResult]:
    """Grade all assertions against a response using hybrid deterministic + Databricks FMAPI approach.

    Strategy:
      1. Check expected_patterns deterministically (regex — zero cost)
      2. Check expected_facts deterministically (substring — zero cost)
      3. Collect: deterministic failures + freeform assertions + guidelines
      4. Grade via Databricks FMAPI (with transcript context for tool-call visibility)

    Args:
        response: The text to check assertions against.
        expected_facts: Exact substrings to check (legacy format).
        expected_patterns: Regex patterns to check (legacy format).
        guidelines: Natural-language guidelines to convert to assertions.
        assertions: Freeform assertion strings for semantic grading.
        judge_model: Databricks model for grading (default: databricks-claude-opus-4-6).
        transcript: Serialized AgentResult.events for transcript-aware grading.

    Returns:
        List of AssertionResult with per-assertion pass/fail and evidence.
    """
    all_results: list[AssertionResult] = []

    # Step 1: Deterministic pattern checks (zero cost)
    pattern_results = _check_patterns(response, expected_patterns or [])
    all_results.extend(pattern_results)

    # Step 2: Deterministic fact checks (zero cost)
    fact_results = _check_facts(response, expected_facts or [])
    all_results.extend(fact_results)

    # Step 3: Collect items for semantic grading
    semantic_items: list[str] = []

    # 3a: Deterministic failures get a second chance via semantic grading
    failed_facts = [r for r in fact_results if not r.passed]
    for r in failed_facts:
        semantic_items.append(f"The response mentions or explains: {r.text}")

    # 3b: Freeform assertions
    if assertions:
        semantic_items.extend(assertions)

    # 3c: Guidelines converted to checkable assertions
    if guidelines:
        for g in guidelines:
            semantic_items.append(f"The response follows this guideline: {g}")

    # Step 4: Agent grading (Databricks FMAPI with transcript context)
    if semantic_items:
        semantic_results = _agent_grade(
            response, semantic_items,
            transcript=transcript,
            judge_model=judge_model,
        )

        # Upgrade deterministic fact failures that pass semantic grading
        fact_upgrade_count = len(failed_facts)
        for i, sr in enumerate(semantic_results[:fact_upgrade_count]):
            if sr.passed:
                # Find the original fact result and upgrade it
                original_fact = failed_facts[i]
                original_fact.passed = True
                original_fact.evidence = f"Semantic match: {sr.evidence}"
                original_fact.method = "semantic"

        # Add freeform assertion results
        freeform_start = fact_upgrade_count
        freeform_end = freeform_start + len(assertions or [])
        for sr in semantic_results[freeform_start:freeform_end]:
            all_results.append(sr)

        # Add guideline results
        guideline_start = freeform_end
        for i, sr in enumerate(semantic_results[guideline_start:]):
            sr.assertion_type = "guideline"
            all_results.append(sr)

    return all_results


# ---------------------------------------------------------------------------
# WITH vs WITHOUT classification
# ---------------------------------------------------------------------------


def _classify_assertion(with_result: AssertionResult, without_result: AssertionResult) -> str:
    """Classify assertion by comparing WITH-skill vs WITHOUT-skill.

    Returns:
        POSITIVE    — fails without skill, passes with (skill is helping)
        REGRESSION  — passes without skill, fails with (skill is hurting)
        NEEDS_SKILL — fails both (skill must add this content)
        NEUTRAL     — same result either way
    """
    if with_result.passed and not without_result.passed:
        return "POSITIVE"
    elif not with_result.passed and without_result.passed:
        return "REGRESSION"
    elif not with_result.passed and not without_result.passed:
        return "NEEDS_SKILL"
    else:
        return "NEUTRAL"


# ---------------------------------------------------------------------------
# GEPA-compatible evaluator
# ---------------------------------------------------------------------------


def grade_with_without(
    with_response: str,
    without_response: str,
    expectations: dict[str, Any],
    judge_model: str | None = None,
    with_transcript: list[dict] | None = None,
    without_transcript: list[dict] | None = None,
) -> tuple[list[AssertionResult], list[AssertionResult], dict[str, Any]]:
    """Grade both WITH and WITHOUT responses and produce GEPA-compatible diagnostics.

    Args:
        with_response: Response generated with skill in context.
        without_response: Response generated without skill (baseline).
        expectations: Dict with expected_facts, expected_patterns, guidelines, assertions.
        judge_model: Databricks model for grading (default: databricks-claude-opus-4-6).

    Returns:
        (with_results, without_results, diagnostics) where diagnostics contains
        per-assertion classifications, effectiveness metrics, and GEPA side_info fields.
    """
    expected_facts = expectations.get("expected_facts", [])
    expected_patterns = expectations.get("expected_patterns", [])
    guidelines = expectations.get("guidelines", [])
    freeform_assertions = expectations.get("assertions", [])

    # Grade WITH-skill response (Databricks FMAPI grading with transcript)
    with_results = grade_assertions(
        with_response,
        expected_facts=expected_facts,
        expected_patterns=expected_patterns,
        guidelines=guidelines,
        assertions=freeform_assertions,
        judge_model=judge_model,
        transcript=with_transcript,
    )

    # Grade WITHOUT-skill response
    # Deterministic checks first (zero cost)
    without_results_deterministic = (
        _check_patterns(without_response, expected_patterns)
        + _check_facts(without_response, expected_facts)
    )

    # For freeform assertions + guidelines on the WITHOUT response,
    # use agent grading (with WITHOUT transcript if available)
    without_semantic_items: list[str] = list(freeform_assertions or [])
    if guidelines:
        for g in guidelines:
            without_semantic_items.append(f"The response follows this guideline: {g}")

    if without_semantic_items:
        without_semantic = _agent_grade(
            without_response, without_semantic_items,
            transcript=without_transcript,
            judge_model=judge_model,
        )
        freeform_count = len(freeform_assertions or [])
        for sr in without_semantic[:freeform_count]:
            without_results_deterministic.append(sr)
        for sr in without_semantic[freeform_count:]:
            sr.assertion_type = "guideline"
            without_results_deterministic.append(sr)

    without_results = without_results_deterministic

    # Compute per-assertion classifications
    # Match by index (both lists follow the same assertion order)
    classifications: list[dict[str, str]] = []
    positives: list[str] = []
    regressions: list[str] = []
    needs_skill: list[str] = []

    min_len = min(len(with_results), len(without_results))
    for i in range(min_len):
        classification = _classify_assertion(with_results[i], without_results[i])
        classifications.append({
            "text": with_results[i].text,
            "classification": classification,
            "with_passed": with_results[i].passed,
            "without_passed": without_results[i].passed,
        })
        if classification == "POSITIVE":
            positives.append(with_results[i].text)
        elif classification == "REGRESSION":
            regressions.append(f"{with_results[i].text} — {with_results[i].evidence}")
        elif classification == "NEEDS_SKILL":
            needs_skill.append(f"{with_results[i].text} — {with_results[i].evidence}")

    # Compute pass rates
    with_passed = sum(1 for r in with_results if r.passed)
    with_total = len(with_results) if with_results else 1
    without_passed = sum(1 for r in without_results if r.passed)
    without_total = len(without_results) if without_results else 1

    pass_rate_with = with_passed / with_total
    pass_rate_without = without_passed / without_total
    effectiveness_delta = pass_rate_with - pass_rate_without
    regression_rate = len(regressions) / with_total if with_total > 0 else 0.0

    # Build diagnostics
    diagnostics: dict[str, Any] = {
        "pass_rate_with": pass_rate_with,
        "pass_rate_without": pass_rate_without,
        "effectiveness_delta": effectiveness_delta,
        "regression_rate": regression_rate,
        "classifications": classifications,
        "positives": positives,
        "regressions": regressions,
        "needs_skill": needs_skill,
    }

    return with_results, without_results, diagnostics


def build_side_info(
    prompt: str,
    with_results: list[AssertionResult],
    without_results: list[AssertionResult],
    diagnostics: dict[str, Any],
    with_response: str = "",
    without_response: str = "",
    reference_answer: str = "",
    human_feedback: str = "",
) -> dict[str, Any]:
    """Build GEPA-compatible side_info dict from grading results.

    GEPA renders each top-level key as a markdown header. Keys are designed
    to give the reflection LM precise, actionable information.
    """
    side_info: dict[str, Any] = {}

    # Task context
    if prompt:
        side_info["Task"] = prompt

    # Per-assertion results — GEPA sees each as an actionable item
    side_info["Assertions"] = [
        {
            "text": r.text,
            "passed": r.passed,
            "evidence": r.evidence,
            "method": r.method,
            "type": r.assertion_type,
        }
        for r in with_results
    ]

    # Failed/passed summaries for quick GEPA scanning
    side_info["Failed_Assertions"] = [
        f"{r.text} — {r.evidence}" for r in with_results if not r.passed
    ]
    side_info["Passed_Assertions"] = [
        f"{r.text} — {r.evidence}" for r in with_results if r.passed
    ]

    # Regressions (assertions that pass WITHOUT but fail WITH)
    if diagnostics.get("regressions"):
        side_info["Regressions"] = diagnostics["regressions"]

    # NEEDS_SKILL items
    if diagnostics.get("needs_skill"):
        side_info["Needs_Skill"] = diagnostics["needs_skill"]

    # Effectiveness metrics
    side_info["Effectiveness"] = {
        "pass_rate_with": diagnostics["pass_rate_with"],
        "pass_rate_without": diagnostics["pass_rate_without"],
        "delta": diagnostics["effectiveness_delta"],
    }

    # Human feedback (injected from feedback.json)
    if human_feedback:
        side_info["Human_Feedback"] = human_feedback

    # Expected vs Actual
    if reference_answer:
        side_info["Expected"] = reference_answer
    if with_response:
        side_info["Actual"] = with_response
        side_info["Actual_Full"] = with_response
    if without_response:
        side_info["Without_Full"] = without_response

    # Diagnostic labels for GEPA
    error_lines: list[str] = []
    for item in diagnostics.get("needs_skill", []):
        error_lines.append(f"NEEDS_SKILL: {item}")
    for item in diagnostics.get("regressions", []):
        error_lines.append(f"REGRESSION: {item}")
    if error_lines:
        side_info["Error"] = "\n".join(error_lines)

    # skill_md_specific_info for component-targeted reflection
    if error_lines:
        side_info["skill_md_specific_info"] = {
            "Assertion_Diagnostics": "\n".join(error_lines),
            "Regressions": "\n".join(diagnostics.get("regressions", [])),
        }

    return side_info


def compute_score(
    diagnostics: dict[str, Any],
    token_efficiency: float = 1.0,
    structure: float = 1.0,
) -> tuple[float, dict[str, float]]:
    """Compute final composite score from grading diagnostics.

    Scoring weights:
      40% effectiveness_delta (WITH vs WITHOUT — primary signal)
      30% pass_rate_with     (absolute quality)
      15% token_efficiency   (smaller candidates score higher)
       5% structure          (syntax validity, zero cost)
     -10% regression_rate    (assertions that regressed)

    Returns:
        (final_score, scores_dict) for GEPA's Pareto frontier.
    """
    effectiveness_delta = diagnostics["effectiveness_delta"]
    pass_rate_with = diagnostics["pass_rate_with"]
    regression_rate = diagnostics["regression_rate"]

    final_score = max(
        0.0,
        min(
            1.0,
            0.40 * effectiveness_delta
            + 0.30 * pass_rate_with
            + 0.15 * token_efficiency
            + 0.05 * structure
            - 0.10 * regression_rate,
        ),
    )

    scores = {
        "pass_rate_with": pass_rate_with,
        "pass_rate_without": diagnostics["pass_rate_without"],
        "effectiveness_delta": effectiveness_delta,
        "regression_rate": regression_rate,
        "token_efficiency": token_efficiency,
        "structure": structure,
        "final": final_score,
    }

    return final_score, scores
