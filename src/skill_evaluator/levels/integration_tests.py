"""Level 2: Integration Tests (#405) — End-to-end Databricks workflow testing.

Tests complete skill executions against a real Databricks workspace with
resource lifecycle management (setup, test, teardown).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .base import EvalLevel, LevelConfig, LevelResult

logger = logging.getLogger(__name__)


class IntegrationTestLevel(EvalLevel):
    """End-to-end integration tests against real Databricks workspace."""

    @property
    def name(self) -> str:
        return "integration"

    @property
    def level_number(self) -> int:
        return 2

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

        # Step 1: Test MCP connectivity
        logger.info("Testing MCP tool connectivity...")
        connectivity_feedbacks = self._test_mcp_connectivity(config)
        feedbacks.extend(connectivity_feedbacks)

        # If MCP connectivity failed, skip integration tests
        mcp_failures = [f for f in connectivity_feedbacks if f["value"] == "fail"]
        if mcp_failures:
            return LevelResult(
                level=self.name,
                score=0.0,
                feedbacks=feedbacks,
                metadata={"skipped": True, "reason": "MCP connectivity failed"},
            )

        # Step 2: Run integration test cases
        integration_cases = config.test_instructions.get_test_cases_by_category("integration")
        if not integration_cases:
            # Fall back to all test cases if none tagged as integration
            integration_cases = config.test_instructions.ground_truth

        if not integration_cases:
            feedbacks.append({
                "name": "integration/no_test_cases",
                "value": "skip",
                "rationale": "No test cases available for integration testing",
                "source": "CODE",
            })
            return LevelResult(level=self.name, score=0.0, feedbacks=feedbacks)

        all_scores = []
        for case in integration_cases:
            prompt = case.inputs.get("prompt", "")
            case_id = case.id
            logger.info(f"Integration test: {case_id}")

            start_time = time.time()
            try:
                result = run_agent_sync_wrapper(
                    prompt=prompt,
                    skill_md=config.skill.skill_md_content,
                    mcp_config=config.mcp_config.servers if config.mcp_config else None,
                    timeout_seconds=config.agent_timeout,
                    model=config.agent_model,
                )
                execution_time = time.time() - start_time

                # Check execution success
                success = result.response_text and len(result.response_text) > 10
                feedbacks.append({
                    "name": f"integration/{case_id}/execution",
                    "value": "pass" if success else "fail",
                    "rationale": f"Agent completed in {execution_time:.1f}s"
                    if success else "Agent returned empty or very short response",
                    "source": "CODE",
                })

                # Check trace-based expectations
                trace_feedbacks = self._check_trace_expectations(case, result)
                feedbacks.extend(trace_feedbacks)

                # Check tool call success rate
                if result.trace_metrics:
                    total_calls = result.trace_metrics.total_tool_calls
                    failed_calls = sum(
                        1 for tc in result.trace_metrics.tool_calls
                        if tc.success is False
                    )
                    success_rate = (total_calls - failed_calls) / total_calls if total_calls > 0 else 1.0
                    feedbacks.append({
                        "name": f"integration/{case_id}/tool_success_rate",
                        "value": "pass" if success_rate >= 0.8 else "fail",
                        "rationale": f"Tool success rate: {success_rate:.0%} ({total_calls - failed_calls}/{total_calls})",
                        "source": "CODE",
                    })

                task_score = 1.0 if success else 0.0
                all_scores.append(task_score)

                task_results.append({
                    "task_id": case_id,
                    "execution_time_s": execution_time,
                    "success": success,
                    "tool_calls": result.trace_metrics.total_tool_calls if result.trace_metrics else 0,
                })

            except Exception as e:
                execution_time = time.time() - start_time
                logger.error(f"Integration test failed for {case_id}: {e}")
                feedbacks.append({
                    "name": f"integration/{case_id}/execution",
                    "value": "fail",
                    "rationale": f"Agent execution failed after {execution_time:.1f}s: {e}",
                    "source": "CODE",
                })
                all_scores.append(0.0)

        score = sum(all_scores) / len(all_scores) if all_scores else 0.0

        return LevelResult(
            level=self.name,
            score=score,
            feedbacks=feedbacks,
            task_results=task_results,
            metadata={
                "num_integration_tests": len(integration_cases),
                "success_rate": score,
            },
        )

    def _test_mcp_connectivity(self, config: LevelConfig) -> list[dict[str, Any]]:
        """Verify MCP servers can be reached."""
        feedbacks = []
        if not config.mcp_config or not config.mcp_config.servers:
            feedbacks.append({
                "name": "integration/mcp_connectivity",
                "value": "fail",
                "rationale": "No MCP servers configured",
                "source": "CODE",
            })
            return feedbacks

        for server_name in config.mcp_config.servers:
            feedbacks.append({
                "name": f"integration/mcp_connectivity/{server_name}",
                "value": "pass",
                "rationale": f"MCP server '{server_name}' configuration found",
                "source": "CODE",
            })

        return feedbacks

    def _check_trace_expectations(self, case, result) -> list[dict[str, Any]]:
        """Check trace-based expectations from ground_truth."""
        feedbacks = []
        trace = result.trace_metrics
        if not trace:
            return feedbacks

        expectations = (case.expectations or {}).get("trace_expectations", {})

        # Required tools
        for tool in expectations.get("required_tools", []):
            found = trace.has_tool(tool)
            feedbacks.append({
                "name": f"integration/{case.id}/required_tool/{tool}",
                "value": "pass" if found else "fail",
                "rationale": f"Required tool '{tool}' {'used' if found else 'NOT used'}",
                "source": "CODE",
            })

        # Banned tools
        for tool in expectations.get("banned_tools", []):
            used = trace.has_tool(tool)
            feedbacks.append({
                "name": f"integration/{case.id}/banned_tool/{tool}",
                "value": "pass" if not used else "fail",
                "rationale": f"Banned tool '{tool}' {'NOT used' if not used else 'was USED'}",
                "source": "CODE",
            })

        return feedbacks
