"""Level 3: Static Skill Eval (#406) — LLM-based skill quality assessment.

Evaluates the SKILL.md document itself against a rubric of 10 criteria
without executing the skill. Deterministic checks run first (zero LLM cost),
then an LLM judge evaluates semantic dimensions.

When L1 (unit tests) has already run, deterministic scores for tool_accuracy
and examples_valid are derived from L1 results to avoid duplicate work.
When L3 runs standalone (e.g., via MCP tool), it runs its own checks.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from .base import EvalLevel, LevelConfig, LevelResult
from .shared_validators import (
    extract_code_blocks,
    check_python_syntax,
    check_sql_syntax,
    check_yaml_syntax,
)

logger = logging.getLogger(__name__)

# The 10 evaluation dimensions from issue #406
STATIC_EVAL_DIMENSIONS = [
    {
        "id": "self_contained",
        "name": "Self-Contained",
        "description": "Skill provides all necessary context without assuming external knowledge",
        "type": "llm",
    },
    {
        "id": "no_conflicts",
        "name": "No Conflicting Information",
        "description": "Instructions are consistent throughout, no contradictions",
        "type": "llm",
    },
    {
        "id": "security",
        "name": "Security",
        "description": "No hardcoded secrets, dangerous commands have warnings, safe defaults",
        "type": "hybrid",
    },
    {
        "id": "llm_navigable",
        "name": "LLM-Navigable Structure",
        "description": "Clear headings, logical flow, easy to find relevant sections",
        "type": "llm",
    },
    {
        "id": "actionable",
        "name": "Actionable Instructions",
        "description": "Concrete steps, not vague guidance",
        "type": "llm",
    },
    {
        "id": "scoped_clearly",
        "name": "Scoped Clearly",
        "description": "Explicit about what the skill does and doesn't do",
        "type": "llm",
    },
    {
        "id": "tool_accuracy",
        "name": "Tools/CLI Accuracy",
        "description": "All mentioned tools, commands, and APIs actually exist",
        "type": "deterministic",
    },
    {
        "id": "examples_valid",
        "name": "Examples Are Valid",
        "description": "Code snippets and examples are syntactically correct",
        "type": "deterministic",
    },
    {
        "id": "error_handling",
        "name": "Error Handling Guidance",
        "description": "What to do when things fail",
        "type": "llm",
    },
    {
        "id": "no_hallucination_triggers",
        "name": "No Hallucination Triggers",
        "description": "Doesn't mention non-existent features or fake endpoints",
        "type": "llm",
    },
]

_STATIC_EVAL_PROMPT = """You are evaluating a Claude Code skill file for quality. Your goal is to determine if this skill would "just work" — if an LLM agent could follow it successfully given only the information inside.

## Skill Content (SKILL.md)
{skill_md}

## Reference Files
{references}

## Available MCP Tools (from the MCP server)
{available_tools}

## Pre-Check Results (Deterministic)
{precheck_results}

## Evaluation Dimensions

For each dimension below, provide:
- **score**: 1-10 (1=critically broken, 5=mediocre, 7=good, 10=excellent)
- **evidence**: Specific quote or observation from the skill (include line references where possible)
- **recommendation**: Actionable improvement suggestion (if score < 7). Be specific — reference the exact section, line, or instruction that needs to change.

### Dimensions to Evaluate:

1. **SELF-CONTAINED**: Does the skill provide all necessary context? Can an agent follow it without needing to look things up externally? Are all APIs, parameters, and patterns fully documented?

2. **NO CONFLICTING INFORMATION**: Are instructions consistent throughout? Does any section contradict another? Are there ambiguous rules that could be interpreted multiple ways?

3. **SECURITY**: Are there hardcoded tokens, secrets, or credentials? Do dangerous operations have appropriate warnings? Are default values safe?

4. **LLM-NAVIGABLE STRUCTURE**: Is the document well-organized with clear headings? Can an LLM quickly find the relevant section for a given task? Is there a logical flow from overview to details?

5. **ACTIONABLE INSTRUCTIONS**: Are instructions concrete ("Call create_or_update_genie with these parameters") vs vague ("set things up appropriately")? Can each step be directly executed?

6. **SCOPED CLEARLY**: Is it explicit about what the skill covers and what it doesn't? Are boundaries clear? Will an agent know when NOT to use this skill?

7. **ERROR HANDLING GUIDANCE**: What should happen when API calls fail? Are common error scenarios addressed? Is there recovery guidance?

8. **NO HALLUCINATION TRIGGERS**: Does the skill reference features, tools, or endpoints that don't exist? Could any instruction cause the agent to hallucinate non-existent capabilities?

## Response Format

Return a JSON array with exactly 8 objects (dimensions 1-6 and 7-8, skipping tool_accuracy and examples_valid which were checked deterministically):

```json
[
  {{"dimension": "self_contained", "score": 8, "evidence": "...", "recommendation": "..."}},
  {{"dimension": "no_conflicts", "score": 9, "evidence": "No contradictions found", "recommendation": null}},
  ...
]
```"""


class StaticEvalLevel(EvalLevel):
    """Evaluate SKILL.md quality without execution."""

    @property
    def name(self) -> str:
        return "static"

    @property
    def level_number(self) -> int:
        return 3

    def run(self, config: LevelConfig) -> LevelResult:
        feedbacks: list[dict[str, Any]] = []
        dimension_scores: dict[str, float] = {}

        # Phase 1: Deterministic checks (zero LLM cost)
        # Reuse L1 results when available to avoid duplicate work
        logger.info("Running deterministic checks...")

        tool_score, tool_feedbacks = self._check_tool_accuracy(config)
        feedbacks.extend(tool_feedbacks)
        dimension_scores["tool_accuracy"] = tool_score

        example_score, example_feedbacks = self._check_examples_valid(config)
        feedbacks.extend(example_feedbacks)
        dimension_scores["examples_valid"] = example_score

        security_feedbacks = self._check_security_deterministic(config)
        feedbacks.extend(security_feedbacks)

        # Phase 2: LLM judge for semantic dimensions
        logger.info("Running LLM judge for semantic dimensions...")
        llm_feedbacks, llm_scores = self._run_llm_judge(config, feedbacks)
        feedbacks.extend(llm_feedbacks)
        dimension_scores.update(llm_scores)

        # Collect recommendations from all feedbacks
        recommendations = []
        for f in feedbacks:
            rationale = f.get("rationale", "")
            if "Recommendation:" in rationale:
                rec = rationale.split("Recommendation:")[-1].strip()
                if rec and rec != "None":
                    recommendations.append(rec)
            elif f.get("value") == "fail" and f.get("source") == "CODE":
                recommendations.append(f"{f.get('name', '')}: {rationale}")

        # Compute overall score with coverage factor
        # When fewer dimensions are evaluated (e.g., LLM unavailable),
        # the score is penalized proportionally to prevent inflation.
        if dimension_scores:
            overall_score_raw = sum(dimension_scores.values()) / len(dimension_scores)
            coverage_factor = len(dimension_scores) / len(STATIC_EVAL_DIMENSIONS)
            score = (overall_score_raw / 10.0) * coverage_factor
        else:
            overall_score_raw = 0.0
            coverage_factor = 0.0
            score = 0.0

        metadata: dict[str, Any] = {
            "overall_score": round(overall_score_raw, 1),
            "criteria": dimension_scores,
            "recommendations": recommendations,
            "dimensions_evaluated": len(dimension_scores),
            "dimensions_total": len(STATIC_EVAL_DIMENSIONS),
            "coverage_factor": round(coverage_factor, 2),
        }

        # Flag if LLM dimensions were skipped
        llm_skipped = any(
            f.get("value") == "skip" and f.get("source") == "LLM_JUDGE"
            for f in feedbacks
        )
        if llm_skipped:
            metadata["llm_skipped"] = True

        return LevelResult(
            level=self.name,
            score=score,
            feedbacks=feedbacks,
            metadata=metadata,
        )

    def _check_tool_accuracy(self, config: LevelConfig) -> tuple[float, list[dict]]:
        """Check that referenced MCP tools actually exist.

        Reuses L1 results when available (orchestrator mode) to avoid
        running the same check twice. Falls back to L1's thorough
        tool reference checker when running standalone.
        """
        # Reuse L1 results if available
        l1_result = config.prior_results.get("unit")
        if l1_result:
            return self._derive_tool_score_from_l1(l1_result)

        # Standalone mode: use L1's thorough tool reference checker
        from .unit_tests import _check_tool_references

        tool_feedbacks = _check_tool_references(config)

        if not tool_feedbacks:
            return 10.0, []

        # Check for skip (no MCP tools available)
        if all(f.get("value") == "skip" for f in tool_feedbacks):
            return 5.0, [{
                "name": "static/tool_accuracy",
                "value": "skip",
                "rationale": "No MCP tools available for verification",
                "source": "CODE",
            }]

        total = sum(1 for f in tool_feedbacks if f["value"] in ("pass", "fail"))
        passed = sum(1 for f in tool_feedbacks if f["value"] == "pass")
        score = (passed / total * 10.0) if total > 0 else 10.0

        # Re-label feedbacks for static namespace
        static_feedbacks = []
        for f in tool_feedbacks:
            static_feedbacks.append({
                **f,
                "name": f["name"].replace("unit/tool_available/", "static/tool_accuracy/"),
            })

        return max(1.0, score), static_feedbacks

    def _derive_tool_score_from_l1(self, l1_result) -> tuple[float, list[dict]]:
        """Derive tool_accuracy score from L1's tool reference feedbacks."""
        tool_feedbacks = [
            f for f in l1_result.feedbacks
            if f.get("name", "").startswith("unit/tool_available")
        ]

        if not tool_feedbacks:
            return 10.0, [{
                "name": "static/tool_accuracy",
                "value": "pass",
                "rationale": "No tool references to check (derived from L1)",
                "source": "CODE",
            }]

        if all(f.get("value") == "skip" for f in tool_feedbacks):
            return 5.0, [{
                "name": "static/tool_accuracy",
                "value": "skip",
                "rationale": "No MCP tools available for verification (derived from L1)",
                "source": "CODE",
            }]

        total = sum(1 for f in tool_feedbacks if f["value"] in ("pass", "fail"))
        passed = sum(1 for f in tool_feedbacks if f["value"] == "pass")
        score = (passed / total * 10.0) if total > 0 else 10.0

        return max(1.0, score), [{
            "name": "static/tool_accuracy",
            "value": "pass" if score >= 6.0 else "fail",
            "rationale": f"Derived from L1: {passed}/{total} tools verified. Score: {score:.1f}/10",
            "source": "CODE",
        }]

    def _check_examples_valid(self, config: LevelConfig) -> tuple[float, list[dict]]:
        """Check that code examples are syntactically valid.

        Reuses L1 results when available (orchestrator mode) to avoid
        running the same check twice. Falls back to running checks
        directly when standalone. Validates Python, SQL, and YAML.
        """
        # Reuse L1 results if available
        l1_result = config.prior_results.get("unit")
        if l1_result:
            return self._derive_examples_score_from_l1(l1_result)

        # Standalone mode: run checks directly
        feedbacks = []
        all_content = {"SKILL.md": config.skill.skill_md_content}
        all_content.update(config.skill.reference_files)

        total = 0
        passed = 0
        for filename, content in all_content.items():
            blocks = extract_code_blocks(content)
            for i, (lang, code) in enumerate(blocks):
                if lang in ("python", "py"):
                    result = check_python_syntax(code)
                    total += 1
                    if result["valid"]:
                        passed += 1
                    feedbacks.append({
                        "name": f"static/examples_valid/{filename}:block_{i+1}",
                        "value": "pass" if result["valid"] else "fail",
                        "rationale": result.get("error", "Valid syntax"),
                        "source": "CODE",
                    })
                elif lang == "sql":
                    result = check_sql_syntax(code)
                    total += 1
                    if result["valid"]:
                        passed += 1
                    feedbacks.append({
                        "name": f"static/examples_valid/{filename}:block_{i+1}",
                        "value": "pass" if result["valid"] else "fail",
                        "rationale": result.get("error", "Valid syntax"),
                        "source": "CODE",
                    })
                elif lang in ("yaml", "yml"):
                    result = check_yaml_syntax(code)
                    total += 1
                    if result["valid"]:
                        passed += 1
                    feedbacks.append({
                        "name": f"static/examples_valid/{filename}:block_{i+1}",
                        "value": "pass" if result["valid"] else "fail",
                        "rationale": result.get("error", "Valid syntax"),
                        "source": "CODE",
                    })

        score = (passed / total * 10.0) if total > 0 else 10.0
        return max(1.0, score), feedbacks

    def _derive_examples_score_from_l1(self, l1_result) -> tuple[float, list[dict]]:
        """Derive examples_valid score from L1's syntax validation feedbacks."""
        syntax_feedbacks = [
            f for f in l1_result.feedbacks
            if f.get("name", "").startswith(("unit/python_syntax/", "unit/sql_syntax/", "unit/yaml_syntax/"))
        ]

        if not syntax_feedbacks:
            return 10.0, [{
                "name": "static/examples_valid",
                "value": "pass",
                "rationale": "No code blocks to validate (derived from L1)",
                "source": "CODE",
            }]

        total = len(syntax_feedbacks)
        passed = sum(1 for f in syntax_feedbacks if f["value"] == "pass")
        score = (passed / total * 10.0) if total > 0 else 10.0

        return max(1.0, score), [{
            "name": "static/examples_valid",
            "value": "pass" if score >= 6.0 else "fail",
            "rationale": f"Derived from L1: {passed}/{total} code blocks valid. Score: {score:.1f}/10",
            "source": "CODE",
        }]

    def _check_security_deterministic(self, config: LevelConfig) -> list[dict]:
        """Scan for hardcoded secrets and credentials."""
        feedbacks = []
        content = config.skill.all_content

        secret_patterns = [
            (r"dapi[a-f0-9]{32,}", "Databricks API token"),
            (r"sk-[a-zA-Z0-9]{32,}", "API key (sk-...)"),
            (r"ghp_[a-zA-Z0-9]{36,}", "GitHub personal access token"),
            (r"Bearer\s+[a-zA-Z0-9\-_.]{20,}", "Bearer token"),
            (r"password\s*=\s*['\"][^'\"]{8,}['\"]", "Hardcoded password"),
        ]

        for pattern, description in secret_patterns:
            matches = re.findall(pattern, content)
            if matches:
                feedbacks.append({
                    "name": f"static/security/{description.lower().replace(' ', '_')}",
                    "value": "fail",
                    "rationale": f"Found potential {description}: {matches[0][:20]}...",
                    "source": "CODE",
                })

        if not any(f["value"] == "fail" for f in feedbacks):
            feedbacks.append({
                "name": "static/security/secrets_scan",
                "value": "pass",
                "rationale": "No hardcoded secrets detected",
                "source": "CODE",
            })

        return feedbacks

    def _run_llm_judge(
        self, config: LevelConfig, precheck_feedbacks: list[dict]
    ) -> tuple[list[dict], dict[str, float]]:
        """Run LLM judge for semantic evaluation dimensions."""
        llm_dimensions = [
            d for d in STATIC_EVAL_DIMENSIONS
            if d["type"] in ("llm", "hybrid")
        ]

        try:
            from ..grading.llm_backend import completion_with_fallback
        except ImportError:
            logger.warning("LLM backend not available — skipping semantic evaluation")
            return self._skip_llm_dimensions(
                llm_dimensions,
                "LLM backend unavailable (openai not installed). Install with: pip install openai",
            )

        # Format precheck results for the prompt
        precheck_text = "\n".join(
            f"- {f['name']}: {f['value']} — {f['rationale']}"
            for f in precheck_feedbacks
        )

        # Format reference files
        ref_text = ""
        for name, content in config.skill.reference_files.items():
            ref_text += f"\n### {name}\n{content[:2000]}\n"

        # Format available tools
        tools_text = "\n".join(
            f"- {t}" for t in (config.mcp_config.available_tools if config.mcp_config else [])
        ) or "No MCP tools available for verification"

        prompt = _STATIC_EVAL_PROMPT.format(
            skill_md=config.skill.skill_md_content,
            references=ref_text or "(no reference files)",
            available_tools=tools_text,
            precheck_results=precheck_text,
        )

        judge_model = config.judge_model or "databricks/databricks-claude-opus-4-6"

        try:
            response = completion_with_fallback(
                model=judge_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
            )

            content = response.choices[0].message.content
            # Extract JSON from response
            json_match = re.search(r"\[.*\]", content, re.DOTALL)
            if not json_match:
                logger.warning("LLM judge did not return valid JSON")
                return self._skip_llm_dimensions(
                    llm_dimensions,
                    "LLM judge returned invalid response (no JSON array found)",
                )

            dimensions = json.loads(json_match.group())

        except Exception as e:
            logger.error(f"LLM judge failed: {e}")
            return self._skip_llm_dimensions(
                llm_dimensions,
                f"LLM judge call failed: {e}",
            )

        feedbacks = []
        scores = {}

        for dim in dimensions:
            dim_id = dim.get("dimension", "unknown")
            dim_score = dim.get("score", 3)
            evidence = dim.get("evidence", "")
            recommendation = dim.get("recommendation")

            scores[dim_id] = float(dim_score)
            feedbacks.append({
                "name": f"static/{dim_id}",
                "value": "pass" if dim_score >= 6 else "fail",
                "rationale": f"Score: {dim_score}/10. {evidence}"
                + (f" Recommendation: {recommendation}" if recommendation else ""),
                "source": "LLM_JUDGE",
            })

        return feedbacks, scores

    def _skip_llm_dimensions(
        self, dimensions: list[dict], reason: str
    ) -> tuple[list[dict], dict[str, float]]:
        """Generate skip feedbacks when LLM evaluation cannot run."""
        feedbacks = []
        for dim in dimensions:
            feedbacks.append({
                "name": f"static/{dim['id']}",
                "value": "skip",
                "rationale": reason,
                "source": "LLM_JUDGE",
            })
        return feedbacks, {}
