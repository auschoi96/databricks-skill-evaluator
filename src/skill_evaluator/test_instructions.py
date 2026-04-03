"""Per-skill evaluation configuration loader.

Loads the eval/ subdirectory inside a skill directory, which contains
ground truth test cases, manifest config, and custom evaluation instructions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from .core.dataset import EvalRecord, YAMLDatasetSource

logger = logging.getLogger(__name__)


@dataclass
class SkillTestInstructions:
    """Evaluation configuration for a specific skill."""

    ground_truth: list[EvalRecord] = field(default_factory=list)
    manifest: dict[str, Any] = field(default_factory=dict)
    thinking_instructions: Optional[str] = None
    output_instructions: Optional[str] = None
    source_of_truth_files: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_skill_dir(cls, skill_dir: Path) -> "SkillTestInstructions":
        """Load evaluation config from a skill's eval/ directory.

        Expected structure:
            skill_dir/
                eval/
                    ground_truth.yaml         # Test cases
                    manifest.yaml             # Scorer config
                    thinking_instructions.md  # L4 custom criteria
                    output_instructions.md    # L5 custom criteria
                    source_of_truth/          # Expected outputs for L5
                        expected_output.json
                        ...
        """
        skill_dir = Path(skill_dir).resolve()
        eval_dir = skill_dir / "eval"

        if not eval_dir.is_dir():
            logger.warning(f"No eval/ directory found in {skill_dir}. Using empty config.")
            return cls()

        # Load ground truth
        ground_truth = []
        gt_path = eval_dir / "ground_truth.yaml"
        if gt_path.exists():
            source = YAMLDatasetSource(gt_path)
            ground_truth = source.load()
            logger.info(f"Loaded {len(ground_truth)} test cases from {gt_path}")

        # Load manifest
        manifest = {}
        manifest_path = eval_dir / "manifest.yaml"
        if manifest_path.exists():
            with open(manifest_path) as f:
                manifest = yaml.safe_load(f) or {}

        # Load thinking instructions
        thinking_instructions = None
        thinking_path = eval_dir / "thinking_instructions.md"
        if thinking_path.exists():
            thinking_instructions = thinking_path.read_text()

        # Load output instructions
        output_instructions = None
        output_path = eval_dir / "output_instructions.md"
        if output_path.exists():
            output_instructions = output_path.read_text()

        # Load source of truth files
        source_of_truth_files = {}
        sot_dir = eval_dir / "source_of_truth"
        if sot_dir.is_dir():
            for f in sorted(sot_dir.iterdir()):
                if f.is_file() and not f.name.startswith("."):
                    source_of_truth_files[f.name] = f.read_text()

        return cls(
            ground_truth=ground_truth,
            manifest=manifest,
            thinking_instructions=thinking_instructions,
            output_instructions=output_instructions,
            source_of_truth_files=source_of_truth_files,
        )

    @property
    def has_ground_truth(self) -> bool:
        return len(self.ground_truth) > 0

    @property
    def has_thinking_eval(self) -> bool:
        return self.thinking_instructions is not None

    @property
    def has_output_eval(self) -> bool:
        return self.output_instructions is not None or len(self.source_of_truth_files) > 0

    def get_test_cases_by_category(self, category: str) -> list[EvalRecord]:
        """Filter test cases by metadata.category."""
        return [
            tc for tc in self.ground_truth
            if tc.metadata and tc.metadata.get("category") == category
        ]


def init_eval_config(skill_dir: Path, skill_name: str) -> Path:
    """Initialize an eval/ directory with template files.

    Creates:
        skill_dir/eval/
            ground_truth.yaml
            manifest.yaml
            thinking_instructions.md
            output_instructions.md
            source_of_truth/

    Returns the eval/ directory path.
    """
    eval_dir = Path(skill_dir) / "eval"
    eval_dir.mkdir(exist_ok=True)

    # ground_truth.yaml template
    gt_path = eval_dir / "ground_truth.yaml"
    if not gt_path.exists():
        gt_content = f"""metadata:
  skill_name: {skill_name}
  version: 0.1.0

test_cases:
  - id: {skill_name}_001
    inputs:
      prompt: "TODO: Add a test prompt here"
    outputs:
      response: "TODO: Add expected response"
    expectations:
      expected_facts:
        - "TODO: Add expected facts"
      expected_patterns:
        - pattern: "TODO"
          min_count: 1
          description: "TODO: Describe what this pattern checks"
      assertions:
        - "TODO: Add freeform assertions"
      guidelines:
        - "TODO: Add quality guidelines"
      trace_expectations:
        required_tools: []
        banned_tools: []
        tool_limits: {{}}
      asset_verification:
        expected_tool_params: {{}}
          # TODO: Add expected tool parameters, e.g.:
          # mcp__databricks__create_or_update_genie:
          #   display_name: "My Space"
          #   table_identifiers: ["catalog.schema.table"]
        assertions:
          - "TODO: Add freeform asset assertions"
        verify_live:
          []
          # TODO: Add live verification checks, e.g.:
          # - resource_type: genie_space
          #   extract_id_from: mcp__databricks__create_or_update_genie
          #   id_field: space_id
          #   checks:
          #     - field: display_name
          #       operator: contains
          #       value: "My Space"
          #     - field: table_identifiers
          #       operator: length_gte
          #       value: 1
      source_of_truth:
        file: ""
          # TODO: Add filename from eval/source_of_truth/, e.g.:
          # file: expected_output.json
        mandatory_facts:
          - "TODO: Add mandatory facts to verify in output"
    metadata:
      category: happy_path
      difficulty: easy
"""
        gt_path.write_text(gt_content)

    # manifest.yaml template
    manifest_path = eval_dir / "manifest.yaml"
    if not manifest_path.exists():
        manifest_content = f"""skill_name: {skill_name}
tool_modules: []
description: "Evaluation config for {skill_name}"

scorers:
  enabled:
    - python_syntax
    - sql_syntax
    - pattern_adherence
    - expected_facts_present
  default_guidelines:
    - "Response must address user's request completely"
    - "Code must follow documented best practices"
  trace_expectations:
    tool_limits:
      Bash: 10
      Read: 20
    token_budget:
      max_total: 100000
    required_tools: []
    banned_tools: []

quality_gates:
  syntax_valid: 1.0
  pattern_adherence: 0.9
"""
        manifest_path.write_text(manifest_content)

    # thinking_instructions.md template
    thinking_path = eval_dir / "thinking_instructions.md"
    if not thinking_path.exists():
        thinking_content = f"""# Thinking Evaluation Criteria for {skill_name}

## Efficiency
- TODO: Define expected tool call count for common tasks
- TODO: Specify which MCP tools should be preferred over Bash/CLI

## Recovery
- TODO: Define expected error recovery behavior
- TODO: What should the agent do when a tool call fails?

## Completeness
- TODO: What steps must the agent complete?
- TODO: What MCP tools must be called?
"""
        thinking_path.write_text(thinking_content)

    # output_instructions.md template
    output_path = eval_dir / "output_instructions.md"
    if not output_path.exists():
        output_content = f"""# Output Evaluation Criteria for {skill_name}

## Expected Artifacts
- TODO: Describe what artifacts the agent should produce

## Mandatory Facts
These are defined per test case in ground_truth.yaml under expectations.expected_facts.

## Comparison Approach
The semantic grader handles comparison. Place expected output files
in eval/source_of_truth/ for artifact-level comparison.
"""
        output_path.write_text(output_content)

    # source_of_truth directory
    sot_dir = eval_dir / "source_of_truth"
    sot_dir.mkdir(exist_ok=True)

    logger.info(f"Initialized eval config in {eval_dir}")
    return eval_dir
