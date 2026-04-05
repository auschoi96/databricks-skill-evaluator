"""Level 5: Output Eval (#408) — Agent output quality vs source of truth.

Evaluates WHAT the agent produces across three dimensions:
1. Response quality: WITH vs WITHOUT skill comparison via semantic grader
2. Asset verification: Did the agent actually create the resources it claimed to?
   - Trace-based: inspect tool call inputs/results from the execution log
   - Live verification: call Databricks SDK to confirm resources actually exist
3. Source of truth: Compare created assets against expected output files

The key insight: evaluating the response text alone isn't enough. A skill like
databricks-genie should be tested by verifying that the Genie Space was actually
created, has the right tables, and includes sample questions — not just that the
agent's text response mentions these things.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

from .base import EvalLevel, LevelConfig, LevelResult

logger = logging.getLogger(__name__)

# Cache for WITHOUT-skill baselines (keyed by prompt hash)
_baseline_cache: dict[str, Any] = {}


class OutputEvalLevel(EvalLevel):
    """Evaluate agent output quality: response text + created assets."""

    @property
    def name(self) -> str:
        return "output"

    @property
    def level_number(self) -> int:
        return 5

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
                feedbacks=[{"name": "output/no_test_cases", "value": "skip",
                            "rationale": "No test cases in ground_truth.yaml", "source": "CODE"}],
            )

        all_scores = []

        for case in test_cases:
            prompt = case.inputs.get("prompt", "")
            case_id = case.id
            expectations = case.expectations or {}
            logger.info(f"Output eval: {case_id}")

            try:
                # ── Phase 1: Run agent WITH skill ──
                with_result = run_agent_sync_wrapper(
                    prompt=prompt,
                    skill_md=config.skill.skill_md_content,
                    mcp_config=config.mcp_config.servers if config.mcp_config else None,
                    timeout_seconds=config.agent_timeout,
                    model=config.agent_model,
                    mlflow_experiment=config.workspace.experiment_path,
                    skill_name=config.skill.name,
                )

                # Capture MLflow trace ID for assessment logging
                if with_result.mlflow_trace_id:
                    trace_ids.append(with_result.mlflow_trace_id)

                # ── Phase 2: Run agent WITHOUT skill (cached baseline) ──
                prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()[:12]
                if prompt_hash in _baseline_cache:
                    without_result = _baseline_cache[prompt_hash]
                    logger.info(f"  Using cached WITHOUT baseline for {case_id}")
                else:
                    without_result = run_agent_sync_wrapper(
                        prompt=prompt,
                        skill_md=None,
                        mcp_config=config.mcp_config.servers if config.mcp_config else None,
                        timeout_seconds=config.agent_timeout,
                        model=config.agent_model,
                        mlflow_experiment=config.workspace.experiment_path,
                        skill_name=config.skill.name,
                    )
                    _baseline_cache[prompt_hash] = without_result

                # ── Phase 3: Response text grading (WITH vs WITHOUT) ──
                response_feedbacks, response_score = self._grade_responses(
                    case_id, with_result, without_result, expectations, config,
                )
                feedbacks.extend(response_feedbacks)

                # ── Phase 4a: Asset verification (trace-based) ──
                asset_feedbacks = self._verify_assets(
                    case_id, with_result, expectations, config,
                )
                feedbacks.extend(asset_feedbacks)

                # ── Phase 4b: Live asset verification (Databricks SDK) ──
                live_feedbacks = self._verify_live_assets(
                    case_id, with_result, expectations, config,
                )
                feedbacks.extend(live_feedbacks)
                asset_feedbacks.extend(live_feedbacks)

                # ── Phase 5: Source of truth comparison ──
                sot_feedbacks = self._compare_source_of_truth(
                    case_id, with_result, expectations, config,
                )
                feedbacks.extend(sot_feedbacks)

                # Compute task score (weighted: 50% response, 30% assets, 20% SoT)
                asset_pass_rate = _pass_rate(asset_feedbacks) if asset_feedbacks else None
                sot_pass_rate = _pass_rate(sot_feedbacks) if sot_feedbacks else None

                if asset_pass_rate is not None and sot_pass_rate is not None:
                    task_score = 0.50 * response_score + 0.30 * asset_pass_rate + 0.20 * sot_pass_rate
                elif asset_pass_rate is not None:
                    task_score = 0.60 * response_score + 0.40 * asset_pass_rate
                else:
                    task_score = response_score

                all_scores.append(task_score)

                task_results.append({
                    "task_id": case_id,
                    "prompt": prompt,
                    "with_response": with_result.response_text[:500],
                    "without_response": without_result.response_text[:500],
                    "response_score": response_score,
                    "asset_verification": asset_pass_rate,
                    "source_of_truth": sot_pass_rate,
                    "final_score": task_score,
                    "mlflow_trace_id": with_result.mlflow_trace_id,
                })

            except Exception as e:
                logger.error(f"Agent execution failed for {case_id}: {e}")
                feedbacks.append({
                    "name": f"output/{case_id}/execution",
                    "value": "fail",
                    "rationale": f"Agent execution failed: {e}",
                    "source": "CODE",
                })
                all_scores.append(0.0)

        score = sum(all_scores) / len(all_scores) if all_scores else 0.0

        return LevelResult(
            level=self.name,
            score=score,
            feedbacks=feedbacks,
            task_results=task_results,
            trace_ids=trace_ids,
            metadata={
                "pass_rate_with": score,
                "num_test_cases": len(test_cases),
                "num_assertions": len([f for f in feedbacks if f["source"] != "CODE" or "asset" in f["name"]]),
                "num_asset_checks": len([f for f in feedbacks if "asset" in f["name"]]),
                "num_live_checks": len([f for f in feedbacks if "/live/" in f["name"]]),
            },
        )

    # ──────────────────────────────────────────────────────────────────
    # Phase 3: Response text grading
    # ──────────────────────────────────────────────────────────────────

    def _grade_responses(
        self, case_id: str, with_result, without_result,
        expectations: dict, config: LevelConfig,
    ) -> tuple[list[dict], float]:
        """Grade response text WITH vs WITHOUT using semantic grader."""
        feedbacks = []

        # Inject output_instructions as additional guidelines for the judge
        grading_expectations = dict(expectations)
        output_instructions = config.test_instructions.output_instructions
        if output_instructions:
            existing_guidelines = list(grading_expectations.get("guidelines", []))
            existing_guidelines.append(f"Follow these output evaluation criteria: {output_instructions}")
            grading_expectations["guidelines"] = existing_guidelines

        try:
            from ..grading.semantic_grader import grade_with_without, compute_score

            with_transcript = None
            if hasattr(with_result, "events") and with_result.events:
                with_transcript = [
                    {"type": e.type, "data": _truncate_event_data(e.data)}
                    for e in with_result.events[:50]
                ]

            with_assertions, without_assertions, diagnostics = grade_with_without(
                with_response=with_result.response_text,
                without_response=without_result.response_text,
                expectations=grading_expectations,
                judge_model=config.judge_model or "databricks/databricks-claude-opus-4-6",
                with_transcript=with_transcript,
            )

            final_score, score_breakdown = compute_score(diagnostics)

            # Build classification lookup from diagnostics
            classifications = diagnostics.get("classifications", [])
            for i, assertion in enumerate(with_assertions):
                classification = "NEUTRAL"
                if i < len(classifications):
                    classification = classifications[i].get("classification", "NEUTRAL")
                feedbacks.append({
                    "name": f"output/{case_id}/response/{assertion.text[:50]}",
                    "value": "pass" if assertion.passed else "fail",
                    "rationale": f"[{classification}] {assertion.evidence}",
                    "source": "LLM_JUDGE",
                })

            return feedbacks, final_score

        except ImportError:
            # Fallback: simple assertion checking
            feedbacks = self._simple_assertion_check(case_id, with_result.response_text, expectations)
            passed = sum(1 for f in feedbacks if f["value"] == "pass")
            total = len(feedbacks)
            return feedbacks, passed / total if total > 0 else 0.0

    # ──────────────────────────────────────────────────────────────────
    # Phase 4: Asset verification
    # ──────────────────────────────────────────────────────────────────

    def _verify_assets(
        self, case_id: str, agent_result, expectations: dict, config: LevelConfig,
    ) -> list[dict[str, Any]]:
        """Verify that the agent actually created the assets it claimed to.

        Inspects the agent's execution trace for MCP tool calls and their results.
        For creation tools (create_or_update_genie, create_or_update_dashboard, etc.),
        extracts the returned resource ID and verifies the resource exists with
        expected properties.

        Also checks:
        - Tool calls succeeded (result is not an error)
        - Required parameters were passed correctly
        - Created resources match expected configuration
        """
        feedbacks = []
        trace = agent_result.trace_metrics if hasattr(agent_result, "trace_metrics") else None
        if not trace:
            return feedbacks

        asset_expectations = expectations.get("asset_verification", {})
        tool_calls = trace.tool_calls

        # ── Check 1: Tool call success ──
        # Every MCP tool call should have succeeded (no errors in result)
        for tc in tool_calls:
            if not tc.is_mcp_tool:
                continue

            if tc.success is False or (tc.result and _is_error_result(tc.result)):
                feedbacks.append({
                    "name": f"output/{case_id}/asset/tool_call_success/{tc.name}",
                    "value": "fail",
                    "rationale": f"Tool call '{tc.name}' failed: {_truncate(tc.result, 200)}",
                    "source": "CODE",
                })
            elif tc.success is True or (tc.result and not _is_error_result(tc.result)):
                feedbacks.append({
                    "name": f"output/{case_id}/asset/tool_call_success/{tc.name}",
                    "value": "pass",
                    "rationale": f"Tool call '{tc.name}' succeeded",
                    "source": "CODE",
                })

        # ── Check 2: Creation tool returned a resource ID ──
        # For tools that create resources, verify an ID was returned
        creation_tools = [
            "mcp__databricks__create_or_update_genie",
            "mcp__databricks__create_or_update_dashboard",
            "mcp__databricks__manage_ka",
            "mcp__databricks__manage_mas",
            "mcp__databricks__create_job",
            "mcp__databricks__create_or_update_pipeline",
            "mcp__databricks__migrate_genie",
        ]

        for tc in tool_calls:
            if tc.name not in creation_tools:
                continue

            result_data = _parse_tool_result(tc.result)
            if result_data is None:
                feedbacks.append({
                    "name": f"output/{case_id}/asset/resource_created/{tc.name}",
                    "value": "fail",
                    "rationale": f"Tool '{tc.name}' did not return parseable result",
                    "source": "CODE",
                })
                continue

            # Look for an ID field in the result
            resource_id = _extract_resource_id(result_data)
            if resource_id:
                feedbacks.append({
                    "name": f"output/{case_id}/asset/resource_created/{tc.name}",
                    "value": "pass",
                    "rationale": f"Resource created with ID: {resource_id}",
                    "source": "CODE",
                })
            else:
                feedbacks.append({
                    "name": f"output/{case_id}/asset/resource_created/{tc.name}",
                    "value": "fail",
                    "rationale": f"Tool '{tc.name}' result has no resource ID: {_truncate(str(result_data), 200)}",
                    "source": "CODE",
                })

        # ── Check 3: Tool input parameters match expectations ──
        # Verify the agent passed the right parameters to creation tools
        param_checks = asset_expectations.get("expected_tool_params", {})
        for tool_name, expected_params in param_checks.items():
            matching_calls = [tc for tc in tool_calls if tc.name == tool_name]
            if not matching_calls:
                feedbacks.append({
                    "name": f"output/{case_id}/asset/params/{tool_name}",
                    "value": "fail",
                    "rationale": f"Expected tool '{tool_name}' was never called",
                    "source": "CODE",
                })
                continue

            tc = matching_calls[-1]  # Check the last call (in case of retries)
            for param_name, expected_value in expected_params.items():
                actual = tc.input.get(param_name)
                if expected_value == "*":
                    # Wildcard — just check the param exists
                    passed = actual is not None
                    feedbacks.append({
                        "name": f"output/{case_id}/asset/params/{tool_name}/{param_name}",
                        "value": "pass" if passed else "fail",
                        "rationale": f"Param '{param_name}' {'present' if passed else 'MISSING'} in {tool_name} call",
                        "source": "CODE",
                    })
                elif isinstance(expected_value, list):
                    # Check that all expected items are in the actual list
                    actual_list = actual if isinstance(actual, list) else []
                    missing = [v for v in expected_value if v not in actual_list]
                    passed = len(missing) == 0
                    feedbacks.append({
                        "name": f"output/{case_id}/asset/params/{tool_name}/{param_name}",
                        "value": "pass" if passed else "fail",
                        "rationale": f"Param '{param_name}': {'all expected values present' if passed else f'missing {missing}'}",
                        "source": "CODE",
                    })
                else:
                    # Exact or substring match
                    passed = str(expected_value).lower() in str(actual).lower() if actual else False
                    feedbacks.append({
                        "name": f"output/{case_id}/asset/params/{tool_name}/{param_name}",
                        "value": "pass" if passed else "fail",
                        "rationale": f"Param '{param_name}': expected '{expected_value}', got '{_truncate(str(actual), 100)}'",
                        "source": "CODE",
                    })

        # ── Check 4: Custom asset verification via LLM ──
        # If output_instructions.md defines asset checks, use LLM to verify
        asset_assertions = asset_expectations.get("assertions", [])
        if asset_assertions and tool_calls:
            llm_feedbacks = self._llm_verify_assets(case_id, tool_calls, asset_assertions, config)
            feedbacks.extend(llm_feedbacks)

        return feedbacks

    def _llm_verify_assets(
        self, case_id: str, tool_calls, assertions: list[str], config: LevelConfig,
    ) -> list[dict[str, Any]]:
        """Use LLM judge to verify asset creation against freeform assertions."""
        try:
            from ..grading.llm_backend import completion_with_fallback
        except ImportError:
            return []

        # Build tool call summary for the LLM
        tool_summary = ""
        for tc in tool_calls:
            if tc.is_mcp_tool:
                tool_summary += f"\n[TOOL_USE] {tc.name}: {json.dumps(tc.input, default=str)[:500]}"
                if tc.result:
                    tool_summary += f"\n[TOOL_RESULT] {_truncate(tc.result, 500)}"

        # Include output_instructions if available
        output_ctx = ""
        if config.test_instructions.output_instructions:
            output_ctx = f"\n## Output Evaluation Criteria\n{config.test_instructions.output_instructions}\n"

        prompt = f"""Verify whether these assertions are satisfied based on the agent's tool calls and their results.
{output_ctx}
## Tool Call Log
{tool_summary}

## Assertions to Verify
{chr(10).join(f'{i}. {a}' for i, a in enumerate(assertions))}

For each assertion, determine if it PASSED or FAILED based on the tool calls above.
Return JSON array:
```json
[{{"index": 0, "passed": true, "evidence": "Tool call created space with 3 tables"}}]
```"""

        try:
            response = completion_with_fallback(
                model=config.judge_model or "databricks/databricks-claude-opus-4-6",
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
            )
            content = response.choices[0].message.content
            json_match = re.search(r"\[.*\]", content, re.DOTALL)
            if not json_match:
                return []

            results = json.loads(json_match.group())
            feedbacks = []
            for r in results:
                idx = r.get("index", 0)
                assertion_text = assertions[idx] if idx < len(assertions) else "unknown"
                feedbacks.append({
                    "name": f"output/{case_id}/asset/assertion/{assertion_text[:40]}",
                    "value": "pass" if r.get("passed") else "fail",
                    "rationale": r.get("evidence", ""),
                    "source": "LLM_JUDGE",
                })
            return feedbacks
        except Exception as e:
            logger.error(f"Asset LLM verification failed: {e}")
            return []

    # ──────────────────────────────────────────────────────────────────
    # Phase 4b: Live asset verification via Databricks SDK
    # ──────────────────────────────────────────────────────────────────

    def _get_workspace_client(self, config: LevelConfig):
        """Create a Databricks WorkspaceClient from the eval config."""
        try:
            from databricks.sdk import WorkspaceClient
        except ImportError:
            logger.warning("databricks-sdk not installed — live verification skipped")
            return None

        try:
            return WorkspaceClient(profile=config.workspace.profile)
        except Exception as e:
            logger.warning(f"Failed to create WorkspaceClient: {e}")
            return None

    def _verify_live_assets(
        self, case_id: str, agent_result, expectations: dict, config: LevelConfig,
    ) -> list[dict[str, Any]]:
        """Verify resources actually exist in Databricks via SDK calls.

        Reads expectations.asset_verification.verify_live from ground_truth.yaml.
        For each entry, extracts the resource ID from the agent's trace and calls
        the Databricks SDK to confirm the resource exists with expected properties.
        """
        feedbacks = []
        asset_expectations = expectations.get("asset_verification", {})
        verify_live = asset_expectations.get("verify_live", [])
        if not verify_live:
            return feedbacks

        trace = agent_result.trace_metrics if hasattr(agent_result, "trace_metrics") else None
        if not trace:
            return feedbacks

        client = self._get_workspace_client(config)
        if client is None:
            feedbacks.append({
                "name": f"output/{case_id}/live/sdk_unavailable",
                "value": "skip",
                "rationale": "Databricks SDK not available for live verification",
                "source": "CODE",
            })
            return feedbacks

        for entry in verify_live:
            resource_type = entry.get("resource_type", "unknown")
            extract_from = entry.get("extract_id_from", "")
            id_field = entry.get("id_field", "id")
            checks = entry.get("checks", [])

            # Extract resource ID from the agent's tool call results
            resource_id = self._extract_id_from_trace(trace, extract_from, id_field)
            if not resource_id:
                feedbacks.append({
                    "name": f"output/{case_id}/live/{resource_type}/id_not_found",
                    "value": "fail",
                    "rationale": f"Could not extract '{id_field}' from '{extract_from}' tool results",
                    "source": "CODE",
                })
                continue

            # Fetch the live resource via SDK
            live_data = self._fetch_live_resource(client, resource_type, resource_id)
            if live_data is None:
                feedbacks.append({
                    "name": f"output/{case_id}/live/{resource_type}/not_found",
                    "value": "fail",
                    "rationale": f"{resource_type} '{resource_id}' does not exist or is inaccessible",
                    "source": "CODE",
                })
                continue

            feedbacks.append({
                "name": f"output/{case_id}/live/{resource_type}/exists",
                "value": "pass",
                "rationale": f"{resource_type} '{resource_id}' exists in workspace",
                "source": "CODE",
            })

            # Run property checks against the live data
            for check in checks:
                check_feedback = self._run_live_check(
                    case_id, resource_type, resource_id, live_data, check,
                )
                feedbacks.append(check_feedback)

        return feedbacks

    def _extract_id_from_trace(self, trace, tool_name: str, id_field: str) -> str | None:
        """Extract a resource ID from a specific tool call's result in the trace."""
        matching_calls = [tc for tc in trace.tool_calls if tc.name == tool_name]
        if not matching_calls:
            return None

        # Check the last call (in case of retries)
        tc = matching_calls[-1]
        result_data = _parse_tool_result(tc.result)
        if not isinstance(result_data, dict):
            return None

        return str(result_data[id_field]) if id_field in result_data else None

    def _fetch_live_resource(self, client, resource_type: str, resource_id: str) -> dict | None:
        """Fetch a resource from Databricks and return its properties as a dict."""
        try:
            if resource_type == "genie_space":
                resource = client.genie.get_space(resource_id)
            elif resource_type == "dashboard":
                resource = client.lakeview.get(resource_id)
            elif resource_type == "job":
                resource = client.jobs.get(int(resource_id))
            elif resource_type == "pipeline":
                resource = client.pipelines.get(resource_id)
            else:
                logger.warning(f"Unsupported resource type for live verification: {resource_type}")
                return None

            # Convert SDK object to dict for uniform property checking
            if hasattr(resource, "as_dict"):
                return resource.as_dict()
            elif hasattr(resource, "__dict__"):
                return {k: v for k, v in resource.__dict__.items() if not k.startswith("_")}
            else:
                return {"_raw": str(resource)}

        except Exception as e:
            logger.warning(f"Failed to fetch {resource_type} '{resource_id}': {e}")
            return None

    def _run_live_check(
        self, case_id: str, resource_type: str, resource_id: str,
        live_data: dict, check: dict,
    ) -> dict[str, Any]:
        """Run a single property check against live resource data."""
        field_name = check.get("field", "")
        operator = check.get("operator", "exists")
        expected = check.get("value")

        # Navigate nested fields (e.g., "config.tables")
        actual = live_data
        for part in field_name.split("."):
            if isinstance(actual, dict):
                actual = actual.get(part)
            else:
                actual = None
                break

        passed = False
        rationale = ""

        if operator == "exists":
            passed = actual is not None
            rationale = f"Field '{field_name}' {'exists' if passed else 'does not exist'}"

        elif operator == "eq":
            passed = str(actual).lower() == str(expected).lower() if actual is not None else False
            rationale = f"Field '{field_name}': expected '{expected}', got '{_truncate(str(actual), 100)}'"

        elif operator == "contains":
            passed = str(expected).lower() in str(actual).lower() if actual is not None else False
            rationale = f"Field '{field_name}': {'contains' if passed else 'does not contain'} '{expected}'"

        elif operator == "length_gte":
            actual_len = len(actual) if hasattr(actual, "__len__") else 0
            passed = actual_len >= int(expected)
            rationale = f"Field '{field_name}': length {actual_len} (need >= {expected})"

        elif operator == "gte":
            try:
                passed = float(actual) >= float(expected) if actual is not None else False
                rationale = f"Field '{field_name}': {actual} >= {expected} = {passed}"
            except (ValueError, TypeError):
                rationale = f"Field '{field_name}': cannot compare {actual} >= {expected}"

        elif operator == "lte":
            try:
                passed = float(actual) <= float(expected) if actual is not None else False
                rationale = f"Field '{field_name}': {actual} <= {expected} = {passed}"
            except (ValueError, TypeError):
                rationale = f"Field '{field_name}': cannot compare {actual} <= {expected}"

        else:
            rationale = f"Unknown operator '{operator}'"

        return {
            "name": f"output/{case_id}/live/{resource_type}/{field_name or 'check'}",
            "value": "pass" if passed else "fail",
            "rationale": rationale,
            "source": "CODE",
        }

    # ──────────────────────────────────────────────────────────────────
    # Phase 5: Source of truth comparison
    # ──────────────────────────────────────────────────────────────────

    def _compare_source_of_truth(
        self, case_id: str, agent_result, expectations: dict, config: LevelConfig,
    ) -> list[dict[str, Any]]:
        """Compare agent output against source of truth files.

        If the skill's eval/source_of_truth/ directory contains expected output
        files and the test case references them, compare the actual agent output
        (response text + tool results) against the expected content.
        """
        feedbacks = []
        sot_config = expectations.get("source_of_truth", {})
        if not sot_config:
            return feedbacks

        sot_file = sot_config.get("file")
        mandatory_facts = sot_config.get("mandatory_facts", [])

        if not sot_file or not config.test_instructions.source_of_truth_files:
            return feedbacks

        expected_content = config.test_instructions.source_of_truth_files.get(sot_file)
        if not expected_content:
            feedbacks.append({
                "name": f"output/{case_id}/sot/file_missing",
                "value": "fail",
                "rationale": f"Source of truth file '{sot_file}' not found in eval/source_of_truth/",
                "source": "CODE",
            })
            return feedbacks

        # Check mandatory facts against agent response + tool results
        actual_content = agent_result.response_text
        if hasattr(agent_result, "trace_metrics") and agent_result.trace_metrics:
            for tc in agent_result.trace_metrics.tool_calls:
                if tc.result:
                    actual_content += f"\n{tc.result}"

        for fact in mandatory_facts:
            found = fact.lower() in actual_content.lower()
            feedbacks.append({
                "name": f"output/{case_id}/sot/fact/{fact[:40]}",
                "value": "pass" if found else "fail",
                "rationale": f"Mandatory fact '{fact}' {'found' if found else 'NOT found'} in output",
                "source": "CODE",
            })

        # Use LLM to compare expected vs actual if both are available
        if expected_content and actual_content:
            llm_feedbacks = self._llm_compare_sot(
                case_id, actual_content[:3000], expected_content[:3000], config,
            )
            feedbacks.extend(llm_feedbacks)

        return feedbacks

    def _llm_compare_sot(
        self, case_id: str, actual: str, expected: str, config: LevelConfig,
    ) -> list[dict[str, Any]]:
        """LLM comparison of actual output vs source of truth."""
        try:
            from ..grading.llm_backend import completion_with_fallback
        except ImportError:
            return []

        prompt = f"""Compare the actual agent output against the expected source of truth.

## Expected Output (Source of Truth)
{expected}

## Actual Agent Output
{actual}

Rate the match on these dimensions:
1. **Structural match**: Does the actual output have the same structure/components?
2. **Content accuracy**: Are the key values/data correct?
3. **Completeness**: Is everything from the expected output present?

Return JSON:
```json
[
  {{"dimension": "structural_match", "score": 8, "evidence": "..."}},
  {{"dimension": "content_accuracy", "score": 7, "evidence": "..."}},
  {{"dimension": "completeness", "score": 9, "evidence": "..."}}
]
```"""

        try:
            response = completion_with_fallback(
                model=config.judge_model or "databricks/databricks-claude-opus-4-6",
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
            )
            content = response.choices[0].message.content
            json_match = re.search(r"\[.*\]", content, re.DOTALL)
            if not json_match:
                return []

            dims = json.loads(json_match.group())
            feedbacks = []
            for d in dims:
                dim_score = d.get("score", 5)
                feedbacks.append({
                    "name": f"output/{case_id}/sot/{d.get('dimension', 'unknown')}",
                    "value": "pass" if dim_score >= 6 else "fail",
                    "rationale": f"Score: {dim_score}/10. {d.get('evidence', '')}",
                    "source": "LLM_JUDGE",
                })
            return feedbacks
        except Exception as e:
            logger.error(f"SoT LLM comparison failed: {e}")
            return []

    # ──────────────────────────────────────────────────────────────────
    # Fallback: simple assertion checking
    # ──────────────────────────────────────────────────────────────────

    def _simple_assertion_check(
        self, case_id: str, response: str, expectations: dict,
    ) -> list[dict[str, Any]]:
        """Fallback assertion checking without the semantic grader."""
        feedbacks = []
        response_lower = response.lower()

        for fact in expectations.get("expected_facts", []):
            found = fact.lower() in response_lower
            feedbacks.append({
                "name": f"output/{case_id}/fact/{fact[:40]}",
                "value": "pass" if found else "fail",
                "rationale": f"Fact '{fact}' {'found' if found else 'NOT found'} in response",
                "source": "CODE",
            })

        for pat_config in expectations.get("expected_patterns", []):
            pattern = pat_config if isinstance(pat_config, str) else pat_config.get("pattern", "")
            min_count = pat_config.get("min_count", 1) if isinstance(pat_config, dict) else 1
            matches = len(re.findall(pattern, response, re.IGNORECASE))
            passed = matches >= min_count
            feedbacks.append({
                "name": f"output/{case_id}/pattern/{pattern[:40]}",
                "value": "pass" if passed else "fail",
                "rationale": f"Pattern '{pattern}': {matches} matches (need >={min_count})",
                "source": "CODE",
            })

        return feedbacks


# ──────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────

def _pass_rate(feedbacks: list[dict]) -> float:
    """Calculate pass rate from a list of feedbacks."""
    if not feedbacks:
        return 0.0
    passed = sum(1 for f in feedbacks if f.get("value") == "pass")
    return passed / len(feedbacks)


def _is_error_result(result: str) -> bool:
    """Check if a tool result string indicates an error."""
    if not result:
        return False
    lower = result.lower()
    return any(marker in lower for marker in ['"error":', "'error':", "traceback", "exception", "failed"])


def _parse_tool_result(result: str | None) -> dict | None:
    """Try to parse a tool result string as a JSON dict."""
    if not result:
        return None
    try:
        parsed = json.loads(result)
        return parsed if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


def _extract_resource_id(data) -> str | None:
    """Extract a resource ID from a tool result dict."""
    if not isinstance(data, dict):
        return None
    for key in ["space_id", "dashboard_id", "job_id", "pipeline_id", "run_id",
                 "id", "resource_id", "assistant_id", "index_name"]:
        if key in data and data[key]:
            return str(data[key])
    return None


def _truncate(text: str, max_len: int) -> str:
    """Truncate text with ellipsis."""
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."


def _truncate_event_data(data: dict, max_str_len: int = 500) -> dict:
    """Truncate string values in event data dict while preserving structure."""
    result = {}
    for k, v in data.items():
        if isinstance(v, str) and len(v) > max_str_len:
            result[k] = v[:max_str_len] + "..."
        elif isinstance(v, dict):
            result[k] = _truncate_event_data(v, max_str_len)
        else:
            result[k] = v
    return result
