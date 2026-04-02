"""Self-contained HTML report generator for skill evaluation.

Extracted from ai-dev-kit/.test/src/skill_test/optimize/html_report.py

Generates a standalone HTML file with:
  - Per-task results: prompt, WITH/WITHOUT responses, assertion pass/fail
  - Assertion highlighting (green=pass, red=fail)
  - POSITIVE/REGRESSION/NEEDS_SKILL classification labels
  - Aggregate score summary
  - Feedback textareas per task (exports to feedback.json via download)

Inspired by Anthropic's skill-creator generate_review.py viewer.
No server, no dependencies -- just a single HTML file.
"""

from __future__ import annotations

import html
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _escape(text: str) -> str:
    """HTML-escape text."""
    return html.escape(text, quote=True)


def generate_report(
    skill_name: str,
    task_results: list[dict[str, Any]],
    output_path: str | Path,
    aggregate_scores: dict[str, float] | None = None,
) -> Path:
    """Generate a self-contained HTML evaluation report.

    Args:
        skill_name: Name of the skill being evaluated.
        task_results: List of per-task result dicts, each containing:
            - task_id: str
            - prompt: str
            - with_response: str
            - without_response: str
            - assertions: list of dicts with text, passed, evidence, classification
            - scores: dict with pass_rate_with, effectiveness_delta, etc.
        output_path: Where to write the HTML file.
        aggregate_scores: Optional aggregate scores dict.

    Returns:
        Path to the generated HTML file.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    task_cards = []
    for i, result in enumerate(task_results):
        task_id = result.get("task_id", f"task-{i}")
        prompt = result.get("prompt", "")
        with_response = result.get("with_response", "")
        without_response = result.get("without_response", "")
        assertions = result.get("assertions", [])
        scores = result.get("scores", {})

        # Build assertion rows
        assertion_rows = []
        for a in assertions:
            cls = a.get("classification", "")
            passed = a.get("passed", False)
            status_class = "pass" if passed else "fail"
            badge_class = cls.lower().replace("_", "-") if cls else status_class

            assertion_rows.append(f"""
            <tr class="{status_class}">
                <td>{'&#10004;' if passed else '&#10008;'}</td>
                <td>{_escape(a.get('text', ''))}</td>
                <td>{_escape(a.get('evidence', ''))}</td>
                <td><span class="badge badge-{badge_class}">{_escape(cls)}</span></td>
            </tr>""")

        pass_rate = scores.get("pass_rate_with", 0)
        delta = scores.get("effectiveness_delta", 0)

        task_cards.append(f"""
        <div class="task-card" id="task-{i}">
            <div class="task-header">
                <h3>Task: {_escape(task_id)}</h3>
                <div class="task-scores">
                    <span class="score">Pass Rate: {pass_rate:.0%}</span>
                    <span class="score delta-{'pos' if delta > 0 else 'neg' if delta < 0 else 'zero'}">
                        Delta: {delta:+.2f}
                    </span>
                </div>
            </div>

            <div class="prompt-box">
                <h4>Prompt</h4>
                <pre>{_escape(prompt)}</pre>
            </div>

            <div class="responses">
                <div class="response-col">
                    <h4>WITH Skill</h4>
                    <pre class="response">{_escape(with_response)}</pre>
                </div>
                <div class="response-col">
                    <h4>WITHOUT Skill (baseline)</h4>
                    <pre class="response">{_escape(without_response)}</pre>
                </div>
            </div>

            <div class="assertions-section">
                <h4>Assertions ({sum(1 for a in assertions if a.get('passed'))}/{len(assertions)} passed)</h4>
                <table class="assertions-table">
                    <thead>
                        <tr><th></th><th>Assertion</th><th>Evidence</th><th>Classification</th></tr>
                    </thead>
                    <tbody>
                        {''.join(assertion_rows)}
                    </tbody>
                </table>
            </div>

            <div class="feedback-section">
                <h4>Feedback</h4>
                <select class="verdict-select" data-task="{_escape(task_id)}">
                    <option value="">-- Select --</option>
                    <option value="good">Good</option>
                    <option value="needs_work">Needs Work</option>
                    <option value="regression">Regression</option>
                </select>
                <textarea class="feedback-text" data-task="{_escape(task_id)}"
                    placeholder="Notes on this test case (optional)..."></textarea>
            </div>
        </div>""")

    # Aggregate summary
    agg_html = ""
    if aggregate_scores:
        agg_items = "".join(
            f"<tr><td>{_escape(k)}</td><td>{v:.3f}</td></tr>"
            for k, v in aggregate_scores.items()
        )
        agg_html = f"""
        <div class="aggregate-summary">
            <h3>Aggregate Scores</h3>
            <table class="summary-table">
                <thead><tr><th>Metric</th><th>Value</th></tr></thead>
                <tbody>{agg_items}</tbody>
            </table>
        </div>"""

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Skill Evaluation: {_escape(skill_name)}</title>
<style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
           background: #f5f5f5; color: #333; padding: 20px; max-width: 1400px; margin: 0 auto; }}
    h1 {{ margin-bottom: 10px; }}
    h3 {{ margin-bottom: 8px; }}
    h4 {{ margin-bottom: 6px; color: #555; }}
    .header {{ margin-bottom: 20px; padding: 20px; background: white; border-radius: 8px;
               box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
    .task-card {{ background: white; border-radius: 8px; padding: 20px; margin-bottom: 20px;
                  box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
    .task-header {{ display: flex; justify-content: space-between; align-items: center;
                    margin-bottom: 15px; padding-bottom: 10px; border-bottom: 1px solid #eee; }}
    .task-scores {{ display: flex; gap: 15px; }}
    .score {{ font-weight: 600; padding: 4px 10px; border-radius: 4px; background: #f0f0f0; }}
    .delta-pos {{ color: #16a34a; background: #f0fdf4; }}
    .delta-neg {{ color: #dc2626; background: #fef2f2; }}
    .delta-zero {{ color: #666; }}
    .prompt-box {{ margin-bottom: 15px; }}
    .prompt-box pre {{ background: #f8f9fa; padding: 12px; border-radius: 6px; white-space: pre-wrap;
                       word-wrap: break-word; font-size: 13px; }}
    .responses {{ display: grid; grid-template-columns: 1fr 1fr; gap: 15px; margin-bottom: 15px; }}
    .response {{ background: #f8f9fa; padding: 12px; border-radius: 6px; white-space: pre-wrap;
                 word-wrap: break-word; font-size: 12px; max-height: 400px; overflow-y: auto; }}
    .assertions-table {{ width: 100%; border-collapse: collapse; margin-bottom: 15px; font-size: 13px; }}
    .assertions-table th {{ text-align: left; padding: 8px; background: #f8f9fa; border-bottom: 2px solid #ddd; }}
    .assertions-table td {{ padding: 8px; border-bottom: 1px solid #eee; }}
    .assertions-table tr.pass td:first-child {{ color: #16a34a; font-weight: bold; }}
    .assertions-table tr.fail td:first-child {{ color: #dc2626; font-weight: bold; }}
    .badge {{ padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }}
    .badge-positive {{ background: #dcfce7; color: #16a34a; }}
    .badge-regression {{ background: #fef2f2; color: #dc2626; }}
    .badge-needs-skill {{ background: #fef9c3; color: #a16207; }}
    .badge-neutral {{ background: #f0f0f0; color: #666; }}
    .badge-pass {{ background: #dcfce7; color: #16a34a; }}
    .badge-fail {{ background: #fef2f2; color: #dc2626; }}
    .feedback-section {{ margin-top: 10px; }}
    .verdict-select {{ padding: 6px; border: 1px solid #ddd; border-radius: 4px; margin-bottom: 8px; }}
    .feedback-text {{ width: 100%; height: 60px; padding: 8px; border: 1px solid #ddd; border-radius: 6px;
                      font-family: inherit; font-size: 13px; resize: vertical; }}
    .summary-table {{ border-collapse: collapse; }}
    .summary-table th, .summary-table td {{ padding: 6px 12px; border: 1px solid #ddd; }}
    .aggregate-summary {{ margin-bottom: 20px; }}
    .export-btn {{ padding: 10px 20px; background: #2563eb; color: white; border: none; border-radius: 6px;
                   font-size: 14px; cursor: pointer; margin-top: 10px; }}
    .export-btn:hover {{ background: #1d4ed8; }}
    .nav-buttons {{ display: flex; gap: 8px; margin-bottom: 15px; }}
    .nav-btn {{ padding: 6px 12px; background: #e5e7eb; border: none; border-radius: 4px; cursor: pointer; }}
    .nav-btn:hover {{ background: #d1d5db; }}
</style>
</head>
<body>
<div class="header">
    <h1>Skill Evaluation: {_escape(skill_name)}</h1>
    <p>{len(task_results)} test cases evaluated</p>
    {agg_html}
    <button class="export-btn" onclick="exportFeedback()">Save Feedback</button>
</div>

{''.join(task_cards)}

<script>
function exportFeedback() {{
    const feedback = [];
    document.querySelectorAll('.task-card').forEach(card => {{
        const textarea = card.querySelector('.feedback-text');
        const select = card.querySelector('.verdict-select');
        const taskId = textarea.dataset.task;
        const notes = textarea.value.trim();
        const verdict = select.value;
        if (notes || verdict) {{
            feedback.push({{ task_id: taskId, notes: notes, verdict: verdict, suggested_changes: '' }});
        }}
    }});

    const blob = new Blob([JSON.stringify(feedback, null, 2)], {{ type: 'application/json' }});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'feedback.json';
    a.click();
    URL.revokeObjectURL(url);
}}

// Keyboard navigation
document.addEventListener('keydown', (e) => {{
    const cards = document.querySelectorAll('.task-card');
    if (e.key === 'ArrowDown' || e.key === 'ArrowRight') {{
        // find next card
    }}
}});
</script>
</body>
</html>"""

    output_path.write_text(html_content, encoding="utf-8")
    logger.info("Generated HTML report: %s", output_path)
    return output_path
