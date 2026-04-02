"""Level 3: Static Skill Eval (#406) — LLM-based skill quality assessment.

Evaluates the SKILL.md document itself against a rubric of 10 criteria
without executing the skill. Deterministic checks run first (zero LLM cost),
then an LLM judge evaluates semantic dimensions.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from .base import EvalLevel, LevelConfig, LevelResult
from .unit_tests import _extract_code_blocks, _check_python_syntax, _check_sql_syntax

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
- **score**: 1-5 (1=very poor, 3=acceptable, 5=excellent)
- **evidence**: Specific quote or observation from the skill
- **recommendation**: Actionable improvement suggestion (if score < 4)

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
  {{"dimension": "self_contained", "score": 4, "evidence": "...", "recommendation": "..."}},
  {{"dimension": "no_conflicts", "score": 5, "evidence": "No contradictions found", "recommendation": null}},
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
        logger.info("Running deterministic checks...")

        # Check tool accuracy
        tool_score, tool_feedbacks = self._check_tool_accuracy(config)
        feedbacks.extend(tool_feedbacks)
        dimension_scores["tool_accuracy"] = tool_score

        # Check examples validity
        example_score, example_feedbacks = self._check_examples_valid(config)
        feedbacks.extend(example_feedbacks)
        dimension_scores["examples_valid"] = example_score

        # Check security (deterministic part)
        security_feedbacks = self._check_security_deterministic(config)
        feedbacks.extend(security_feedbacks)

        # Phase 2: LLM judge for semantic dimensions
        logger.info("Running LLM judge for semantic dimensions...")
        llm_feedbacks, llm_scores = self._run_llm_judge(config, feedbacks)
        feedbacks.extend(llm_feedbacks)
        dimension_scores.update(llm_scores)

        # Compute overall score (average of all dimensions, normalized to 0-1)
        if dimension_scores:
            raw_avg = sum(dimension_scores.values()) / len(dimension_scores)
            score = raw_avg / 5.0  # Normalize from 1-5 to 0-1
        else:
            score = 0.0

        return LevelResult(
            level=self.name,
            score=score,
            feedbacks=feedbacks,
            metadata={
                "dimension_scores": dimension_scores,
                "dimensions_evaluated": len(dimension_scores),
            },
        )

    def _check_tool_accuracy(self, config: LevelConfig) -> tuple[float, list[dict]]:
        """Check that referenced MCP tools actually exist."""
        feedbacks = []
        if not config.mcp_config or not config.mcp_config.available_tools:
            # Can't verify — give neutral score
            return 3.0, [{
                "name": "static/tool_accuracy",
                "value": "skip",
                "rationale": "No MCP tools available for verification",
                "source": "CODE",
            }]

        available = set(config.mcp_config.available_tools)
        referenced = config.skill.mcp_tool_references
        missing = [t for t in referenced if t not in available and f"mcp__databricks__{t}" not in available]

        for tool in referenced:
            found = tool in available or f"mcp__databricks__{tool}" in available
            feedbacks.append({
                "name": f"static/tool_accuracy/{tool}",
                "value": "pass" if found else "fail",
                "rationale": f"Tool '{tool}' {'found' if found else 'NOT found'} in MCP server",
                "source": "CODE",
            })

        if not referenced:
            return 5.0, feedbacks

        score = (len(referenced) - len(missing)) / len(referenced) * 5.0
        return max(1.0, score), feedbacks

    def _check_examples_valid(self, config: LevelConfig) -> tuple[float, list[dict]]:
        """Check that code examples are syntactically valid."""
        feedbacks = []
        all_content = {"SKILL.md": config.skill.skill_md_content}
        all_content.update(config.skill.reference_files)

        total = 0
        passed = 0
        for filename, content in all_content.items():
            blocks = _extract_code_blocks(content)
            for i, (lang, code) in enumerate(blocks):
                if lang in ("python", "py"):
                    result = _check_python_syntax(code)
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
                    result = _check_sql_syntax(code)
                    total += 1
                    if result["valid"]:
                        passed += 1

        score = (passed / total * 5.0) if total > 0 else 5.0
        return max(1.0, score), feedbacks

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
        try:
            from ..grading.llm_backend import completion_with_fallback
        except ImportError:
            logger.warning("LLM backend not available — skipping semantic evaluation")
            return [], {}

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

        judge_model = config.judge_model or "databricks/databricks-claude-sonnet-4-6"

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
                return [], {}

            dimensions = json.loads(json_match.group())

        except Exception as e:
            logger.error(f"LLM judge failed: {e}")
            return [], {}

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
                "value": "pass" if dim_score >= 3 else "fail",
                "rationale": f"Score: {dim_score}/5. {evidence}"
                + (f" Recommendation: {recommendation}" if recommendation else ""),
                "source": "LLM_JUDGE",
            })

        return feedbacks, scores
