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

        # Pass prior level results so subsequent levels can reuse them
        level_config.prior_results = dict(result.level_results)

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

        # Log feedbacks as MLflow assessments on traces
        _log_feedbacks_to_trace(level_name, level_result)

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


# Value mapping between level convention and MLflow convention
_LEVEL_TO_MLFLOW = {"pass": "yes", "fail": "no", "skip": None}


def _log_feedbacks_to_trace(level_name: str, level_result: LevelResult) -> None:
    """Log level feedbacks as MLflow Feedback assessments on agent traces.

    This makes evaluation results visible in MLflow's trace UI, following
    the MLflow LLM-as-a-judge pattern (mlflow.org/blog/evaluating-skills-mlflow).
    """
    if not level_result.trace_ids:
        return

    try:
        from mlflow import MlflowClient
        from mlflow.entities import Feedback

        client = MlflowClient()

        for trace_id in level_result.trace_ids:
            for fb_dict in level_result.feedbacks:
                name = fb_dict.get("name", "unknown")
                value = _LEVEL_TO_MLFLOW.get(fb_dict.get("value", ""), fb_dict.get("value"))
                rationale = fb_dict.get("rationale", "")
                source = fb_dict.get("source", "CODE")

                try:
                    client.log_feedback(
                        trace_id=trace_id,
                        name=f"{level_name}/{name}",
                        value=value,
                        rationale=rationale,
                        source=source,
                    )
                except Exception:
                    # Individual feedback logging failure shouldn't stop the loop
                    pass

        logger.debug(
            f"Logged {len(level_result.feedbacks)} feedbacks to "
            f"{len(level_result.trace_ids)} traces for {level_name}"
        )
    except ImportError:
        logger.debug("MLflow not available for feedback logging")
    except Exception as e:
        logger.debug(f"Feedback logging to traces failed: {e}")


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
    """Build self-contained HTML evaluation report with SkillForge-style UI."""
    import html as html_mod
    from .reporting._styles import THEME_CSS, THEME_JS, SVG_ICONS, score_color, score_color_class

    def _esc(text: str) -> str:
        return html_mod.escape(str(text), quote=True)

    def _source_badge(source: str) -> str:
        if source == "LLM_JUDGE":
            return '<span class="badge-llm">LLM</span>'
        return '<span class="badge-code">CODE</span>'

    def _status_badge(value: str) -> str:
        cls = {"pass": "badge-pass", "fail": "badge-fail"}.get(value, "badge-skip")
        return f'<span class="badge {cls}">{_esc(value.upper())}</span>'

    def _classification_badge(rationale: str) -> str:
        for cls_name, css in [
            ("POSITIVE", "badge-positive"), ("REGRESSION", "badge-regression"),
            ("NEEDS_SKILL", "badge-needs-skill"), ("NEUTRAL", "badge-neutral"),
        ]:
            if cls_name in rationale:
                return f'<span class="badge {css}">{cls_name}</span>'
        return ""

    # ── Score bar chart ──
    score_bars_html = ""
    for level_name in _LEVEL_ORDER:
        if level_name not in result.level_results:
            continue
        lr = result.level_results[level_name]
        lvl_num = _LEVEL_ORDER.index(level_name) + 1
        pct = lr.score * 100
        color = score_color(lr.score)
        score_bars_html += f"""
        <div class="score-bar-row">
            <span class="score-bar-label">L{lvl_num} {_esc(level_name)}</span>
            <div class="score-bar-track">
                <div class="score-bar-fill" style="width:{pct:.0f}%;background:{color}"></div>
            </div>
            <span class="score-bar-value" style="color:{color}">{lr.score:.0%}</span>
        </div>"""

    # ── Level detail cards ──
    level_cards = ""
    for level_name in _LEVEL_ORDER:
        if level_name not in result.level_results:
            continue
        lr = result.level_results[level_name]
        lvl_num = _LEVEL_ORDER.index(level_name) + 1
        color = score_color(lr.score)
        meta = lr.metadata or {}

        # Level-specific header stats
        header_stats = ""
        if level_name == "unit":
            blocks = meta.get("code_blocks_tested", 0)
            errors = meta.get("syntax_errors", 0)
            header_stats = f'<span style="font-size:11px;color:var(--text-muted);margin-left:auto">{blocks} blocks tested &middot; {errors} errors</span>'
        elif level_name == "integration":
            n_tests = meta.get("num_integration_tests", 0)
            success_rate = meta.get("success_rate", 0)
            header_stats = f'<span style="font-size:11px;color:var(--text-muted);margin-left:auto">{n_tests} tests &middot; {success_rate:.0%} success</span>'
        elif level_name == "static":
            dims_eval = meta.get("dimensions_evaluated", 0)
            dims_total = meta.get("dimensions_total", 10)
            coverage = meta.get("coverage_factor", 0)
            header_stats = f'<span style="font-size:11px;color:var(--text-muted);margin-left:auto">{dims_eval}/{dims_total} dims &middot; {coverage:.0%} coverage</span>'
        elif level_name == "output":
            n_cases = meta.get("num_test_cases", 0)
            n_asset = meta.get("num_asset_checks", 0)
            n_live = meta.get("num_live_checks", 0)
            header_stats = f'<span style="font-size:11px;color:var(--text-muted);margin-left:auto">{n_cases} cases &middot; {n_asset} asset &middot; {n_live} live checks</span>'

        # Level-specific rich content
        rich_content = ""

        # L3: Dimension score bars
        if level_name == "static" and meta.get("criteria"):
            dim_bars = ""
            for dim_id, dim_score in sorted(meta["criteria"].items()):
                pct = (dim_score / 10.0) * 100
                dim_color = score_color(dim_score / 10.0)
                dim_bars += f"""
                <div class="dim-bar-row">
                    <span class="dim-bar-label">{_esc(dim_id.replace('_', ' ').title())}</span>
                    <div class="dim-bar-track">
                        <div class="dim-bar-fill" style="width:{pct:.0f}%;background:{dim_color}"></div>
                    </div>
                    <span class="dim-bar-value" style="color:{dim_color}">{dim_score:.1f}/10</span>
                </div>"""
            rich_content += f'<div style="margin:8px 0">{dim_bars}</div>'

            # Recommendations
            recs = meta.get("recommendations", [])
            if recs:
                rec_items = "".join(
                    f'<div class="card-row"><span style="color:var(--accent);flex-shrink:0">+</span>'
                    f'<span style="font-size:11px;color:var(--text-secondary);line-height:1.5">{_esc(r)}</span></div>'
                    for r in recs
                )
                rich_content += f"""
                <div class="card card-accent" style="margin-top:8px">
                    <div class="card-header">
                        <svg class="icon"><use href="#icon-bulb"/></svg>
                        <span class="card-title">Recommendations</span>
                    </div>
                    <div class="card-body">{rec_items}</div>
                </div>"""

        # L2: Task results table
        if level_name == "integration" and lr.task_results:
            rows = ""
            for tr in lr.task_results:
                status = "PASS" if tr.get("success") else "FAIL"
                status_cls = "badge-pass" if tr.get("success") else "badge-fail"
                trace_link = tr.get("mlflow_trace_id", "")
                trace_cell = f'<span class="mono" style="font-size:10px">{_esc(trace_link[:12])}</span>' if trace_link else "-"
                rows += f"""
                <tr>
                    <td><span class="mono">{_esc(tr.get('task_id', ''))}</span></td>
                    <td class="mono">{tr.get('execution_time_s', 0):.1f}s</td>
                    <td class="mono">{tr.get('tool_calls', 0)}</td>
                    <td><span class="badge {status_cls}">{status}</span></td>
                    <td>{trace_cell}</td>
                </tr>"""
            rich_content += f"""
            <table style="margin:8px 0">
                <tr><th>Task</th><th>Time</th><th>Tools</th><th>Status</th><th>Trace</th></tr>
                {rows}
            </table>"""

        # L4: Per-task dimension scores
        if level_name == "thinking" and lr.task_results:
            for tr in lr.task_results:
                task_id = tr.get("task_id", "unknown")
                dims = tr.get("dimension_scores", {})
                trace_sum = tr.get("trace_summary", {})
                dim_bars = ""
                for dim_id, dim_score in sorted(dims.items()):
                    pct = (dim_score / 5.0) * 100
                    dim_color = score_color(dim_score / 5.0)
                    dim_bars += f"""
                    <div class="dim-bar-row">
                        <span class="dim-bar-label">{_esc(dim_id.title())}</span>
                        <div class="dim-bar-track">
                            <div class="dim-bar-fill" style="width:{pct:.0f}%;background:{dim_color}"></div>
                        </div>
                        <span class="dim-bar-value" style="color:{dim_color}">{dim_score:.0f}/5</span>
                    </div>"""
                trace_info = ""
                if trace_sum:
                    trace_info = f'<div class="card-row" style="margin-top:6px;padding-top:6px;border-top:1px solid var(--border)"><span class="card-label">Trace</span><span class="card-value">{trace_sum.get("tool_calls", 0)} tools &middot; {trace_sum.get("tokens", 0)} tokens</span></div>'
                rich_content += f"""
                <details style="margin-top:8px">
                    <summary><span class="mono" style="flex:1">{_esc(task_id)}</span></summary>
                    <div class="details-body">
                        {dim_bars}
                        {trace_info}
                    </div>
                </details>"""

        # L5: Per-task expandable sections with WITH/WITHOUT + score breakdown
        if level_name == "output" and lr.task_results:
            for tr in lr.task_results:
                task_id = tr.get("task_id", "unknown")
                final = tr.get("final_score", 0)
                resp_score = tr.get("response_score", 0)
                asset_score = tr.get("asset_verification")
                sot_score = tr.get("source_of_truth")
                final_color = score_color(final)

                # Mini metrics
                mini = f"""
                <div class="mini-metrics">
                    <div class="mini-metric">
                        <div class="mini-metric-value" style="color:{score_color(resp_score)}">{resp_score:.0%}</div>
                        <div class="mini-metric-label">Response</div>
                    </div>"""
                if asset_score is not None:
                    mini += f"""
                    <div class="mini-metric">
                        <div class="mini-metric-value" style="color:{score_color(asset_score)}">{asset_score:.0%}</div>
                        <div class="mini-metric-label">Assets</div>
                    </div>"""
                if sot_score is not None:
                    mini += f"""
                    <div class="mini-metric">
                        <div class="mini-metric-value" style="color:{score_color(sot_score)}">{sot_score:.0%}</div>
                        <div class="mini-metric-label">Source of Truth</div>
                    </div>"""
                mini += f"""
                    <div class="mini-metric">
                        <div class="mini-metric-value" style="color:{final_color}">{final:.0%}</div>
                        <div class="mini-metric-label">Final (weighted)</div>
                    </div>
                </div>"""

                # WITH/WITHOUT comparison
                with_resp = _esc(tr.get("with_response", "(no response)"))
                without_resp = _esc(tr.get("without_response", "(no response)"))
                comparison = f"""
                <div class="output-comparison" style="margin:10px 0">
                    <div class="output-pane">
                        <div class="output-pane-header"><span>WITH Skill</span></div>
                        <div class="output-pane-content"><pre>{with_resp}</pre></div>
                    </div>
                    <div class="output-pane">
                        <div class="output-pane-header"><span>WITHOUT Skill</span></div>
                        <div class="output-pane-content"><pre>{without_resp}</pre></div>
                    </div>
                </div>"""

                # Task-specific feedbacks
                task_prefix = f"output/{task_id}/"
                task_feedbacks = [f for f in lr.feedbacks if f.get("name", "").startswith(task_prefix)]
                fb_rows = ""
                for f in task_feedbacks:
                    rationale = f.get("rationale", "")
                    fb_rows += f"""
                    <tr>
                        <td>{_status_badge(f['value'])}</td>
                        <td style="font-size:11px">{_esc(f.get('name', '').replace(task_prefix, ''))}</td>
                        <td style="font-size:11px">{_esc(rationale[:300])}</td>
                        <td>{_classification_badge(rationale)} {_source_badge(f.get('source', 'CODE'))}</td>
                    </tr>"""

                fb_table = ""
                if fb_rows:
                    fb_table = f"""
                    <table style="margin-top:10px">
                        <tr><th>Status</th><th>Check</th><th>Details</th><th>Type</th></tr>
                        {fb_rows}
                    </table>"""

                rich_content += f"""
                <details style="margin-top:8px">
                    <summary>
                        <span class="mono" style="flex:1">{_esc(task_id)}</span>
                        <span class="badge badge-score badge-{score_color_class(final)}">{final:.0%}</span>
                    </summary>
                    <div class="details-body">
                        {mini}
                        {comparison}
                        {fb_table}
                    </div>
                </details>"""

        # Feedback table (all levels — for L5, show only non-task-specific or if no task_results)
        show_all_feedbacks = level_name != "output" or not lr.task_results
        if show_all_feedbacks:
            feedback_rows = ""
            for f in lr.feedbacks[:100]:
                feedback_rows += f"""
                <tr>
                    <td>{_status_badge(f['value'])}</td>
                    <td style="font-size:11px">{_esc(f.get('name', ''))}</td>
                    <td style="font-size:11px">{_esc(f.get('rationale', '')[:300])}</td>
                    <td>{_source_badge(f.get('source', 'CODE'))}</td>
                </tr>"""
            if feedback_rows:
                rich_content += f"""
                <table style="margin-top:8px">
                    <tr><th>Status</th><th>Check</th><th>Details</th><th>Source</th></tr>
                    {feedback_rows}
                </table>"""

        level_cards += f"""
        <div class="section">
            <div class="section-title">Level {lvl_num}: {_esc(level_name.upper())}</div>
            <div class="card">
                <div class="card-header">
                    <svg class="icon"><use href="#icon-{'check' if lr.passed else 'x'}"/></svg>
                    <span class="card-title">{_esc(level_name.title())}</span>
                    <span class="badge badge-score badge-{score_color_class(lr.score)}">{lr.score:.0%}</span>
                    {header_stats}
                </div>
                <div class="card-body">
                    {rich_content}
                </div>
            </div>
        </div>"""

    # ── Suggestions ──
    suggestions_html = ""
    if result.suggestions:
        sug_rows = ""
        for s in result.suggestions:
            # Extract category tag
            tag = ""
            for prefix, css in [("NEEDS_SKILL", "badge-needs-skill"), ("REGRESSION", "badge-regression"),
                                ("QUALITY", "badge-warn"), ("FAILURE", "badge-fail")]:
                if s.startswith(prefix):
                    tag = f'<span class="badge {css}" style="margin-right:6px">{prefix}</span>'
                    s = s[len(prefix):].lstrip(" [:").lstrip("]").lstrip(": ")
                    break
            sug_rows += f"""
            <div class="card-row" style="align-items:flex-start;gap:8px">
                {tag}<span style="font-size:11px;color:var(--text-secondary);line-height:1.5">{_esc(s)}</span>
            </div>"""
        suggestions_html = f"""
        <div class="section">
            <div class="section-title">Improvement Suggestions</div>
            <div class="card card-accent">
                <div class="card-header">
                    <svg class="icon"><use href="#icon-bulb"/></svg>
                    <span class="card-title">Suggestions</span>
                </div>
                <div class="card-body">{sug_rows}</div>
            </div>
        </div>"""

    # ── Assemble full HTML ──
    total_checks = sum(len(r.feedbacks) for r in result.level_results.values())
    composite_cls = score_color_class(result.composite_score)

    html = f"""<!DOCTYPE html>
<html lang="en" data-theme="dbx-dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="color-scheme" content="dark light">
<title>Skill Evaluation: {_esc(config.skill.name)}</title>
<style>{THEME_CSS}</style>
</head>
<body>
{SVG_ICONS}
<script>{THEME_JS}</script>

<div class="top-bar">
    <span class="top-bar-title">Skill Evaluation: {_esc(config.skill.name)}</span>
    <span class="top-bar-meta">{datetime.now().strftime('%Y-%m-%d %H:%M')} &middot; MLflow: {_esc(result.mlflow_run_id or 'N/A')}</span>
    <button id="theme-toggle" class="btn-secondary" onclick="toggleTheme()">DBX Dark</button>
</div>

<div class="section">
    <div class="section-title">Summary</div>
    <div class="metric-grid">
        <div class="card">
            <div class="card-header">
                <svg class="icon"><use href="#icon-gauge"/></svg>
                <span class="card-title">Composite Score</span>
            </div>
            <div class="hero-metric color-{composite_cls}">{result.composite_score:.0%}</div>
        </div>
        <div class="card">
            <div class="card-header">
                <svg class="icon"><use href="#icon-layers"/></svg>
                <span class="card-title">Levels Run</span>
            </div>
            <div class="hero-metric">{len(result.level_results)}</div>
        </div>
        <div class="card">
            <div class="card-header">
                <svg class="icon"><use href="#icon-clock"/></svg>
                <span class="card-title">Duration</span>
            </div>
            <div class="hero-metric">{result.duration_seconds:.0f}s</div>
        </div>
        <div class="card">
            <div class="card-header">
                <svg class="icon"><use href="#icon-hash"/></svg>
                <span class="card-title">Total Checks</span>
            </div>
            <div class="hero-metric">{total_checks}</div>
        </div>
    </div>
</div>

<div class="section">
    <div class="section-title">Level Scores</div>
    <div class="card">
        <div class="score-bars">{score_bars_html}</div>
    </div>
</div>

{level_cards}

{suggestions_html}

<div class="section">
    <div class="section-title">Human Feedback</div>
    <div class="card">
        <p style="font-size:12px;color:var(--text-muted);margin-bottom:8px">Review the results and provide feedback:</p>
        <textarea id="feedback-notes" placeholder="Enter your feedback here..."></textarea>
        <div style="display:flex;gap:8px;margin-top:8px;align-items:center">
            <select id="feedback-verdict">
                <option value="good">Good</option>
                <option value="needs_work">Needs Work</option>
                <option value="regression">Regression</option>
            </select>
            <button class="btn-primary" onclick="saveFeedback()">Save Feedback</button>
        </div>
    </div>
</div>

<script>
function saveFeedback() {{
    var feedback = {{
        skill_name: "{_esc(config.skill.name)}",
        timestamp: new Date().toISOString(),
        verdict: document.getElementById('feedback-verdict').value,
        notes: document.getElementById('feedback-notes').value,
        composite_score: {result.composite_score},
        level_scores: {json.dumps({k: v.score for k, v in result.level_results.items()})},
    }};
    var blob = new Blob([JSON.stringify(feedback, null, 2)], {{type: 'application/json'}});
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = 'feedback.json';
    a.click();
}}
</script>
</body>
</html>"""

    return html
