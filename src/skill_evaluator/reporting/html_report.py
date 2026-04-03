"""Self-contained HTML report generator for skill evaluation.

Extracted from ai-dev-kit/.test/src/skill_test/optimize/html_report.py

Generates a standalone HTML file with:
  - Per-task results: prompt, WITH/WITHOUT responses, assertion pass/fail
  - Assertion highlighting (green=pass, red=fail)
  - POSITIVE/REGRESSION/NEEDS_SKILL classification labels
  - Aggregate score summary
  - Feedback textareas per task (exports to feedback.json via download)

Uses SkillForge-inspired dark theme with 3-theme toggle.
No server, no dependencies -- just a single HTML file.
"""

from __future__ import annotations

import html
import json
import logging
from pathlib import Path
from typing import Any

from ._styles import THEME_CSS, THEME_JS, SVG_ICONS, score_color, score_color_class

logger = logging.getLogger(__name__)


def _escape(text: str) -> str:
    """HTML-escape text."""
    return html.escape(str(text), quote=True)


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

    # ── Task navigation pills ──
    nav_pills = ""
    for i, result in enumerate(task_results):
        task_id = result.get("task_id", f"task-{i}")
        nav_pills += f'<a href="#task-{i}" class="badge badge-neutral" style="cursor:pointer;text-decoration:none">{_escape(task_id)}</a> '

    # ── Task cards ──
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
            status_badge = f'<span class="badge badge-pass">&#10004;</span>' if passed else f'<span class="badge badge-fail">&#10008;</span>'

            # Classification badge
            cls_lower = cls.lower().replace("_", "-")
            cls_badge = f'<span class="badge badge-{cls_lower}">{_escape(cls)}</span>' if cls else ""

            assertion_rows.append(f"""
            <tr>
                <td>{status_badge}</td>
                <td style="font-size:11px">{_escape(a.get('text', ''))}</td>
                <td style="font-size:11px">{_escape(a.get('evidence', ''))}</td>
                <td>{cls_badge}</td>
            </tr>""")

        pass_rate = scores.get("pass_rate_with", 0)
        delta = scores.get("effectiveness_delta", 0)
        pass_rate_cls = score_color_class(pass_rate)
        delta_cls = "badge-pass" if delta > 0 else "badge-fail" if delta < 0 else "badge-neutral"

        assertions_passed = sum(1 for a in assertions if a.get("passed"))

        task_cards.append(f"""
        <div class="card" id="task-{i}" style="gap:12px;padding:16px">
            <div style="display:flex;justify-content:space-between;align-items:center;padding-bottom:8px;border-bottom:1px solid var(--border)">
                <span style="font-size:13px;font-weight:600;color:var(--text-primary)" class="mono">{_escape(task_id)}</span>
                <div style="display:flex;gap:6px">
                    <span class="badge badge-{pass_rate_cls}">Pass: {pass_rate:.0%}</span>
                    <span class="badge {delta_cls}">Delta: {delta:+.2f}</span>
                </div>
            </div>

            <div>
                <div class="section-title" style="margin-bottom:6px">Prompt</div>
                <pre>{_escape(prompt)}</pre>
            </div>

            <div>
                <div class="section-title" style="margin-bottom:6px">Responses</div>
                <div class="output-comparison">
                    <div class="output-pane">
                        <div class="output-pane-header"><span>WITH Skill</span></div>
                        <div class="output-pane-content"><pre>{_escape(with_response)}</pre></div>
                    </div>
                    <div class="output-pane">
                        <div class="output-pane-header"><span>WITHOUT Skill (baseline)</span></div>
                        <div class="output-pane-content"><pre>{_escape(without_response)}</pre></div>
                    </div>
                </div>
            </div>

            <div>
                <div class="section-title" style="margin-bottom:6px">Assertions ({assertions_passed}/{len(assertions)} passed)</div>
                <table>
                    <thead>
                        <tr><th style="width:40px"></th><th>Assertion</th><th>Evidence</th><th style="width:110px">Classification</th></tr>
                    </thead>
                    <tbody>
                        {''.join(assertion_rows)}
                    </tbody>
                </table>
            </div>

            <div>
                <div class="section-title" style="margin-bottom:6px">Feedback</div>
                <select class="verdict-select" data-task="{_escape(task_id)}">
                    <option value="">-- Select --</option>
                    <option value="good">Good</option>
                    <option value="needs_work">Needs Work</option>
                    <option value="regression">Regression</option>
                </select>
                <textarea class="feedback-text" data-task="{_escape(task_id)}"
                    placeholder="Notes on this test case (optional)..." style="margin-top:6px"></textarea>
            </div>
        </div>""")

    # ── Aggregate summary ──
    agg_html = ""
    if aggregate_scores:
        agg_cards = ""
        for k, v in aggregate_scores.items():
            cls = score_color_class(v) if 0 <= v <= 1 else ""
            agg_cards += f"""
            <div class="card">
                <div class="card-title" style="font-size:11px">{_escape(k.replace('_', ' ').title())}</div>
                <div class="hero-metric{' color-' + cls if cls else ''}" style="font-size:22px">{v:.3f}</div>
            </div>"""
        agg_html = f"""
        <div class="section">
            <div class="section-title">Aggregate Scores</div>
            <div class="metric-grid">{agg_cards}</div>
        </div>"""

    html_content = f"""<!DOCTYPE html>
<html lang="en" data-theme="dbx-dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="color-scheme" content="dark light">
<title>Skill Evaluation: {_escape(skill_name)}</title>
<style>{THEME_CSS}</style>
</head>
<body>
{SVG_ICONS}
<script>{THEME_JS}</script>

<div class="top-bar">
    <span class="top-bar-title">Skill Evaluation: {_escape(skill_name)}</span>
    <span style="font-size:12px;color:var(--text-muted)">{len(task_results)} test cases</span>
    <span style="flex:1"></span>
    <button class="btn-primary" onclick="exportFeedback()">Save Feedback</button>
    <button id="theme-toggle" class="btn-secondary" onclick="toggleTheme()">DBX Dark</button>
</div>

{agg_html}

<div style="display:flex;gap:4px;flex-wrap:wrap;padding:8px 0;position:sticky;top:44px;z-index:10;background:var(--bg-l0)">
    {nav_pills}
</div>

{''.join(task_cards)}

<script>
function exportFeedback() {{
    var feedback = [];
    document.querySelectorAll('.card[id^="task-"]').forEach(function(card) {{
        var textarea = card.querySelector('.feedback-text');
        var select = card.querySelector('.verdict-select');
        var taskId = textarea.dataset.task;
        var notes = textarea.value.trim();
        var verdict = select.value;
        if (notes || verdict) {{
            feedback.push({{ task_id: taskId, notes: notes, verdict: verdict, suggested_changes: '' }});
        }}
    }});

    var blob = new Blob([JSON.stringify(feedback, null, 2)], {{ type: 'application/json' }});
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = 'feedback.json';
    a.click();
    URL.revokeObjectURL(url);
}}

document.addEventListener('keydown', function(e) {{
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
