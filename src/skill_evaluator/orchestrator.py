"""Orchestrator (#409) — ties all 5 evaluation levels together.

Runs enabled levels in dependency order, logs results to Databricks MLflow,
generates HTML reports, and provides self-improvement suggestions.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .auth import WorkspaceConfig
from .levels.base import EvalLevel, LevelConfig, LevelResult
from .mcp_resolver import MCPConfig
from .skill_discovery import SkillDescriptor
from .test_instructions import SkillTestInstructions

logger = logging.getLogger(__name__)


@dataclass
class EvaluationSuiteConfig:
    """Full configuration for an evaluation suite run."""

    workspace: WorkspaceConfig
    skill: SkillDescriptor
    test_instructions: SkillTestInstructions
    mcp_config: Optional[MCPConfig] = None
    levels: list[str] = field(default_factory=lambda: ["unit", "static", "thinking", "output"])
    agent_model: Optional[str] = None
    agent_timeout: int = 300
    judge_model: Optional[str] = None
    parallel_agents: int = 2
    suggest_improvements: bool = False
    compare_baseline_run_id: Optional[str] = None


@dataclass
class EvaluationSuiteResult:
    """Result from running a full evaluation suite."""

    skill_name: str
    level_results: dict[str, LevelResult] = field(default_factory=dict)
    composite_score: float = 0.0
    mlflow_run_id: Optional[str] = None
    suggestions: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_name": self.skill_name,
            "composite_score": self.composite_score,
            "duration_seconds": self.duration_seconds,
            "mlflow_run_id": self.mlflow_run_id,
            "levels": {
                name: result.to_dict()
                for name, result in self.level_results.items()
            },
            "suggestions": self.suggestions,
        }


# Level registry — maps name to class
def _get_level_classes() -> dict[str, type[EvalLevel]]:
    from .levels.unit_tests import UnitTestLevel
    from .levels.integration_tests import IntegrationTestLevel
    from .levels.static_eval import StaticEvalLevel
    from .levels.thinking_eval import ThinkingEvalLevel
    from .levels.output_eval import OutputEvalLevel

    return {
        "unit": UnitTestLevel,
        "integration": IntegrationTestLevel,
        "static": StaticEvalLevel,
        "thinking": ThinkingEvalLevel,
        "output": OutputEvalLevel,
    }


# Execution order (levels without agent first, then agent-based)
_LEVEL_ORDER = ["unit", "static", "integration", "thinking", "output"]


def run_evaluation_suite(config: EvaluationSuiteConfig) -> EvaluationSuiteResult:
    """Run the full evaluation suite.

    Executes levels in dependency order, logs to MLflow, generates reports.
    """
    start_time = time.time()
    result = EvaluationSuiteResult(skill_name=config.skill.name)

    level_classes = _get_level_classes()

    # Resolve "all" to all levels
    requested = config.levels
    if "all" in requested:
        requested = list(_LEVEL_ORDER)

    # Order by dependency
    ordered = [l for l in _LEVEL_ORDER if l in requested]

    # Build shared LevelConfig
    level_config = LevelConfig(
        workspace=config.workspace,
        skill=config.skill,
        test_instructions=config.test_instructions,
        mcp_config=config.mcp_config,
        agent_model=config.agent_model,
        agent_timeout=config.agent_timeout,
        judge_model=config.judge_model,
        parallel_agents=config.parallel_agents,
    )

    # Setup MLflow
    mlflow_run_id = _setup_mlflow(config)
    result.mlflow_run_id = mlflow_run_id

    # Run each level
    for level_name in ordered:
        if level_name not in level_classes:
            logger.warning(f"Unknown level: {level_name}, skipping")
            continue

        level = level_classes[level_name]()
        logger.info(f"\n{'='*60}")
        logger.info(f"Running Level {level.level_number}: {level.name.upper()}")
        logger.info(f"{'='*60}")

        level_start = time.time()
        try:
            level_result = level.run(level_config)
            level_duration = time.time() - level_start
            logger.info(
                f"  Score: {level_result.score:.2f} | "
                f"Feedbacks: {len(level_result.feedbacks)} | "
                f"Duration: {level_duration:.1f}s"
            )
        except Exception as e:
            level_duration = time.time() - level_start
            logger.error(f"  Level {level_name} FAILED: {e}")
            level_result = LevelResult(
                level=level_name,
                score=0.0,
                feedbacks=[{
                    "name": f"{level_name}/error",
                    "value": "fail",
                    "rationale": str(e),
                    "source": "CODE",
                }],
                metadata={"error": str(e)},
            )

        result.level_results[level_name] = level_result

        # Log to MLflow
        _log_level_to_mlflow(level_name, level_result)

    # Compute composite score
    if result.level_results:
        scores = [r.score for r in result.level_results.values()]
        result.composite_score = sum(scores) / len(scores)

    result.duration_seconds = time.time() - start_time

    # Generate suggestions
    if config.suggest_improvements:
        result.suggestions = _generate_suggestions(result)

    # Log composite to MLflow
    _log_suite_to_mlflow(result)

    # Generate HTML report
    _generate_report(config, result)

    logger.info(f"\n{'='*60}")
    logger.info(f"EVALUATION COMPLETE")
    logger.info(f"  Composite score: {result.composite_score:.2f}")
    logger.info(f"  Duration: {result.duration_seconds:.1f}s")
    logger.info(f"  MLflow run: {result.mlflow_run_id}")
    logger.info(f"{'='*60}")

    return result


def _setup_mlflow(config: EvaluationSuiteConfig) -> Optional[str]:
    """Setup MLflow experiment and create parent run."""
    try:
        import mlflow

        mlflow.set_tracking_uri(f"databricks://{config.workspace.profile}")
        mlflow.set_experiment(config.workspace.experiment_path)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_name = f"{config.skill.name}_eval_{timestamp}"

        run = mlflow.start_run(run_name=run_name)
        mlflow.set_tags({
            "skill_name": config.skill.name,
            "eval_type": "suite",
            "levels": ",".join(config.levels),
            "agent_model": config.agent_model or "default",
            "judge_model": config.judge_model or "default",
            "framework_version": "0.1.0",
        })

        return run.info.run_id
    except Exception as e:
        logger.warning(f"MLflow setup failed (continuing without logging): {e}")
        return None


def _log_level_to_mlflow(level_name: str, result: LevelResult) -> None:
    """Log level results as child run metrics."""
    try:
        import mlflow

        mlflow.log_metric(f"L{_LEVEL_ORDER.index(level_name)+1}/{level_name}/score", result.score)

        # Log dimension scores if available
        if result.metadata:
            for key, value in result.metadata.items():
                if isinstance(value, (int, float)):
                    mlflow.log_metric(f"L{_LEVEL_ORDER.index(level_name)+1}/{level_name}/{key}", value)
                elif isinstance(value, dict):
                    for sub_key, sub_value in value.items():
                        if isinstance(sub_value, (int, float)):
                            mlflow.log_metric(
                                f"L{_LEVEL_ORDER.index(level_name)+1}/{level_name}/{sub_key}",
                                sub_value,
                            )
    except Exception as e:
        logger.debug(f"MLflow metric logging failed: {e}")


def _log_suite_to_mlflow(result: EvaluationSuiteResult) -> None:
    """Log suite-level metrics and close the run."""
    try:
        import mlflow

        mlflow.log_metric("suite/composite_score", result.composite_score)
        mlflow.log_metric("suite/duration_seconds", result.duration_seconds)
        mlflow.log_metric("suite/num_levels", len(result.level_results))

        for level_name, level_result in result.level_results.items():
            mlflow.log_metric(f"suite/{level_name}_score", level_result.score)

        # Log evaluation.json as artifact
        eval_json = json.dumps(result.to_dict(), indent=2, default=str)
        with open("/tmp/dse_evaluation.json", "w") as f:
            f.write(eval_json)
        mlflow.log_artifact("/tmp/dse_evaluation.json", "evaluation")

        mlflow.end_run()
    except Exception as e:
        logger.debug(f"MLflow suite logging failed: {e}")


def _generate_suggestions(result: EvaluationSuiteResult) -> list[str]:
    """Analyze evaluation results and generate improvement suggestions."""
    suggestions = []

    for level_name, level_result in result.level_results.items():
        if level_result.score >= 0.9:
            continue

        # Collect failed feedbacks
        failures = [f for f in level_result.feedbacks if f.get("value") == "fail"]

        for failure in failures[:5]:  # Top 5 per level
            name = failure.get("name", "unknown")
            rationale = failure.get("rationale", "")

            if "NEEDS_SKILL" in rationale:
                suggestions.append(
                    f"NEEDS_SKILL [{level_name}]: {rationale}"
                )
            elif "REGRESSION" in rationale:
                suggestions.append(
                    f"REGRESSION [{level_name}]: {rationale}"
                )
            elif level_name == "static":
                suggestions.append(
                    f"QUALITY [{level_name}]: {name} — {rationale}"
                )
            else:
                suggestions.append(
                    f"FAILURE [{level_name}]: {name} — {rationale}"
                )

    return suggestions


def _generate_report(config: EvaluationSuiteConfig, result: EvaluationSuiteResult) -> None:
    """Generate HTML report for human review."""
    try:
        report_path = config.skill.path / "eval" / "report.html"
        report_path.parent.mkdir(parents=True, exist_ok=True)

        html = _build_html_report(config, result)
        report_path.write_text(html)
        logger.info(f"HTML report written to {report_path}")

        # Also log to MLflow
        try:
            import mlflow
            mlflow.log_artifact(str(report_path), "reports")
        except Exception:
            pass

    except Exception as e:
        logger.warning(f"Report generation failed: {e}")


def _build_html_report(config: EvaluationSuiteConfig, result: EvaluationSuiteResult) -> str:
    """Build self-contained HTML evaluation report."""
    level_cards = ""
    for level_name in _LEVEL_ORDER:
        if level_name not in result.level_results:
            continue
        lr = result.level_results[level_name]
        score_color = "#4caf50" if lr.score >= 0.8 else "#ff9800" if lr.score >= 0.5 else "#f44336"

        # Build feedback rows
        feedback_rows = ""
        for f in lr.feedbacks[:50]:
            status_color = "#4caf50" if f["value"] == "pass" else "#f44336" if f["value"] == "fail" else "#9e9e9e"
            feedback_rows += f"""
            <tr>
                <td><span style="color:{status_color};font-weight:bold">{f['value'].upper()}</span></td>
                <td style="font-size:0.85em">{f.get('name','')}</td>
                <td style="font-size:0.85em">{f.get('rationale','')[:200]}</td>
            </tr>"""

        level_cards += f"""
        <div class="level-card">
            <h3>Level {_LEVEL_ORDER.index(level_name)+1}: {level_name.upper()}
                <span style="float:right;color:{score_color};font-size:1.2em">{lr.score:.0%}</span>
            </h3>
            <table>
                <tr><th>Status</th><th>Check</th><th>Details</th></tr>
                {feedback_rows}
            </table>
        </div>"""

    suggestion_items = ""
    for s in result.suggestions:
        suggestion_items += f"<li>{s}</li>"

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Skill Evaluation: {config.skill.name}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 1200px; margin: 0 auto; padding: 20px; background: #f5f5f5; }}
h1 {{ color: #1a1a2e; }}
.summary {{ background: white; padding: 20px; border-radius: 8px; margin-bottom: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
.summary-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 15px; }}
.metric {{ text-align: center; }}
.metric-value {{ font-size: 2em; font-weight: bold; }}
.metric-label {{ font-size: 0.85em; color: #666; }}
.level-card {{ background: white; padding: 20px; border-radius: 8px; margin-bottom: 15px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
th, td {{ padding: 8px 12px; text-align: left; border-bottom: 1px solid #eee; }}
th {{ background: #f9f9f9; font-weight: 600; }}
.suggestions {{ background: #fff3e0; padding: 15px; border-radius: 8px; margin-top: 20px; }}
.suggestions h3 {{ margin-top: 0; }}
.feedback-section {{ background: white; padding: 20px; border-radius: 8px; margin-top: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
.feedback-section textarea {{ width: 100%; height: 80px; margin: 10px 0; }}
.feedback-section button {{ background: #1a73e8; color: white; border: none; padding: 10px 20px; border-radius: 4px; cursor: pointer; }}
</style>
</head>
<body>
<h1>Skill Evaluation: {config.skill.name}</h1>
<p style="color:#666">Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} | MLflow run: {result.mlflow_run_id or 'N/A'}</p>

<div class="summary">
<h2>Summary</h2>
<div class="summary-grid">
    <div class="metric">
        <div class="metric-value" style="color:{'#4caf50' if result.composite_score >= 0.8 else '#ff9800' if result.composite_score >= 0.5 else '#f44336'}">{result.composite_score:.0%}</div>
        <div class="metric-label">Composite Score</div>
    </div>
    <div class="metric">
        <div class="metric-value">{len(result.level_results)}</div>
        <div class="metric-label">Levels Run</div>
    </div>
    <div class="metric">
        <div class="metric-value">{result.duration_seconds:.0f}s</div>
        <div class="metric-label">Duration</div>
    </div>
    <div class="metric">
        <div class="metric-value">{sum(len(r.feedbacks) for r in result.level_results.values())}</div>
        <div class="metric-label">Total Checks</div>
    </div>
</div>
</div>

{level_cards}

{'<div class="suggestions"><h3>Improvement Suggestions</h3><ul>' + suggestion_items + '</ul></div>' if result.suggestions else ''}

<div class="feedback-section">
<h3>Human Feedback</h3>
<p>Review the results above and provide feedback for optimization:</p>
<textarea id="feedback-notes" placeholder="Enter your feedback here..."></textarea>
<br>
<select id="feedback-verdict">
    <option value="good">Good — skill works well</option>
    <option value="needs_work">Needs Work — improvements needed</option>
    <option value="regression">Regression — skill is hurting</option>
</select>
<button onclick="saveFeedback()">Save Feedback</button>
</div>

<script>
function saveFeedback() {{
    const feedback = {{
        skill_name: "{config.skill.name}",
        timestamp: new Date().toISOString(),
        verdict: document.getElementById('feedback-verdict').value,
        notes: document.getElementById('feedback-notes').value,
        composite_score: {result.composite_score},
        level_scores: {json.dumps({k: v.score for k, v in result.level_results.items()})},
    }};
    const blob = new Blob([JSON.stringify(feedback, null, 2)], {{type: 'application/json'}});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'feedback.json';
    a.click();
}}
</script>
</body>
</html>"""

    return html
