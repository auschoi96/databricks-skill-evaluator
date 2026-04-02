"""Human feedback integration for GEPA optimization.

Extracted from ai-dev-kit/.test/src/skill_test/optimize/feedback.py

Bridges the evaluation step (Step 1) to the optimization step (Step 2)
by loading human feedback from feedback.json and formatting it as GEPA
background context.

The feedback format follows Anthropic's skill-creator pattern:
  evaluate -> human reviews HTML report -> exports feedback.json -> GEPA reads it

Feedback is injected into GEPA's ``background`` parameter alongside
existing context (baseline scores, assessment summaries, focus areas).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class FeedbackRecord:
    """A single piece of human feedback for a test case."""

    task_id: str
    notes: str = ""  # Human's review notes
    verdict: str = ""  # "good" | "needs_work" | "regression" | ""
    suggested_changes: str = ""  # What the human thinks should change


def load_feedback(path: str | Path) -> list[FeedbackRecord]:
    """Load human feedback from a JSON file.

    Supports two formats:

    1. Anthropic-style feedback.json (from HTML viewer export):
       {"reviews": [{"run_id": "...", "feedback": "...", "timestamp": "..."}]}

    2. Simple format:
       [{"task_id": "...", "notes": "...", "verdict": "...", "suggested_changes": "..."}]
    """
    path = Path(path)
    if not path.exists():
        logger.warning("Feedback file not found: %s", path)
        return []

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load feedback from %s: %s", path, e)
        return []

    records: list[FeedbackRecord] = []

    # Anthropic-style format
    if isinstance(data, dict) and "reviews" in data:
        for review in data["reviews"]:
            feedback_text = review.get("feedback", "").strip()
            if not feedback_text:
                continue  # Empty feedback = user thought it was fine
            records.append(
                FeedbackRecord(
                    task_id=review.get("run_id", review.get("eval_id", "")),
                    notes=feedback_text,
                )
            )
        return records

    # Simple format
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            records.append(
                FeedbackRecord(
                    task_id=item.get("task_id", ""),
                    notes=item.get("notes", ""),
                    verdict=item.get("verdict", ""),
                    suggested_changes=item.get("suggested_changes", ""),
                )
            )
        return records

    logger.warning("Unrecognized feedback format in %s", path)
    return []


def feedback_to_gepa_background(records: list[FeedbackRecord]) -> str:
    """Format human feedback as GEPA background context.

    Incorporates Anthropic's skill improvement principles:
    - Generalize from feedback, don't overfit to test cases
    - Keep the prompt lean, remove what isn't pulling weight
    - Explain the why, not rigid MUSTs
    - Look for repeated patterns to bundle
    """
    if not records:
        return ""

    lines: list[str] = []
    lines.append("## Human Review Feedback")
    lines.append("")
    lines.append(
        "A human reviewed the skill's outputs and provided the following feedback. "
        "Use this to guide your improvements, but generalize — don't add narrow "
        "fixes for specific test cases. Instead, identify underlying patterns and "
        "address the root cause."
    )
    lines.append("")

    # Improvement principles (from Anthropic's skill-creator)
    lines.append("### Improvement principles")
    lines.append("- **Generalize**: If feedback says 'missing X in test 3', the fix is to teach X broadly, not to add a special case for test 3")
    lines.append("- **Stay lean**: Remove content that isn't helping. If the skill is causing confusion, cut the confusing parts")
    lines.append("- **Explain why**: Prefer reasoning over rigid rules. 'Use X because Y' is better than 'ALWAYS use X'")
    lines.append("- **Bundle patterns**: If multiple test cases hit the same issue, address it once clearly")
    lines.append("")

    # Per-task feedback
    needs_work = [r for r in records if r.verdict == "needs_work" or (not r.verdict and r.notes)]
    regressions = [r for r in records if r.verdict == "regression"]

    if regressions:
        lines.append("### Regressions (skill is hurting)")
        for r in regressions:
            lines.append(f"- **{r.task_id}**: {r.notes}")
            if r.suggested_changes:
                lines.append(f"  Suggested fix: {r.suggested_changes}")
        lines.append("")

    if needs_work:
        lines.append("### Needs improvement")
        for r in needs_work:
            lines.append(f"- **{r.task_id}**: {r.notes}")
            if r.suggested_changes:
                lines.append(f"  Suggested fix: {r.suggested_changes}")
        lines.append("")

    return "\n".join(lines)


def save_feedback(records: list[FeedbackRecord], path: str | Path) -> None:
    """Save feedback records to JSON."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = [
        {
            "task_id": r.task_id,
            "notes": r.notes,
            "verdict": r.verdict,
            "suggested_changes": r.suggested_changes,
        }
        for r in records
    ]
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    logger.info("Saved %d feedback records to %s", len(records), path)
