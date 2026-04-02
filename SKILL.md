---
name: skill-evaluator
description: "Evaluate and optimize Claude Code skills using the 5-level evaluation pyramid. Use when asked to evaluate a skill, test skill quality, check if a SKILL.md works, score a skill, audit skill content, or optimize a skill. Calls skill-evaluator MCP tools to authenticate, discover, test, and score skills against real Databricks workspaces."
---

# Skill Evaluator

Evaluate Claude Code skills across 5 levels — from syntax checks to full agent-based testing — using MCP tools.

## When to Use

Use this skill when the user wants to:
- Evaluate or test a Claude Code skill
- Check if a SKILL.md is well-written or "just works"
- Score a skill against quality criteria
- Run static analysis on skill content
- Compare agent behavior WITH vs WITHOUT a skill
- Optimize or improve a SKILL.md

## The 5-Level Evaluation Pyramid

| Level | Tool | What It Tests | Agent? | Time |
|-------|------|---------------|--------|------|
| L1 | `run_unit_tests` | Code block syntax, broken links | No | Seconds |
| L2 | `run_integration_tests` | End-to-end Databricks workflows | Yes | Minutes |
| L3 | `run_static_eval` | SKILL.md quality (10 criteria, 1-10 scale) | No | Seconds |
| L4 | `run_thinking_eval` | Agent reasoning: efficiency, clarity, recovery | Yes | Minutes |
| L5 | `run_output_eval` | WITH vs WITHOUT skill comparison | Yes | Minutes |

**Start with L1 + L3** (cheap, fast, no agent). Only run L2/L4/L5 if the user wants deep evaluation.

## MCP Tools

| Tool | Purpose |
|------|---------|
| `authenticate_workspace` | Connect to Databricks, save config |
| `discover_skill` | Parse a skill directory, show metadata |
| `init_eval_config` | Scaffold eval/ templates (user fills in TODOs) |
| `run_unit_tests` | L1: Validate code block syntax |
| `run_static_eval` | L3: LLM judge scores SKILL.md quality (1-10 per criteria) |
| `run_integration_tests` | L2: Real agent against Databricks |
| `run_thinking_eval` | L4: Agent reasoning quality from traces |
| `run_output_eval` | L5: WITH/WITHOUT skill comparison |
| `generate_report` | Create HTML report from collected results |
| `run_optimization` | GEPA optimization (future) |

## Workflow

Follow these steps in order:

### Step 0: Authentication

If the user hasn't authenticated yet, call `authenticate_workspace` with their Databricks profile, catalog, and schema. This saves config to `~/.dse/config.yaml` for all subsequent calls.

### Step 1: Discover the Skill

Call `discover_skill` with the skill directory path. Check the response:
- If `has_eval_config` is false, call `init_eval_config` and tell the user they need to fill in the TODO placeholders in `ground_truth.yaml`, `thinking_instructions.md`, and `output_instructions.md` before running evaluation.
- If `has_eval_config` is true, proceed to evaluation.

### Step 2: Run Cheap Levels First

Call `run_unit_tests` and `run_static_eval`. These are fast and don't need agent execution.

**Interpret the results:**
- Unit tests: Look for `value: "fail"` feedbacks — these are broken code blocks or dead links.
- Static eval: Check `metadata.criteria` for per-dimension 1-10 scores. Check `metadata.recommendations` for specific improvement suggestions. Focus on any dimension scoring below 6.

Present a summary table of scores to the user. If there are recommendations, show them.

### Step 3: Run Agent-Based Levels (if requested)

Warn the user that L2/L4/L5 run real Claude Code agents and take several minutes per test case. Then run them in order:

1. `run_integration_tests` — tests real tool connectivity and execution
2. `run_thinking_eval` — evaluates reasoning quality
3. `run_output_eval` — the core WITH/WITHOUT comparison

For L4 and L5, the `mcp_json_path` parameter should point to the `.mcp.json` file that configures the Databricks MCP server (the one the skill's tools use). If omitted, the tool auto-discovers it from parent directories.

### Step 4: Generate Report

Call `generate_report` with all collected results as a JSON dict:
```json
{
  "unit": { ...result from run_unit_tests... },
  "static": { ...result from run_static_eval... },
  "thinking": { ...result from run_thinking_eval... },
  "output": { ...result from run_output_eval... }
}
```

This creates an HTML report at `eval/report.html` with interactive feedback controls.

### Step 5: Interpret and Suggest

Based on the results, provide the user with:
1. **Summary**: Composite score and per-level breakdown
2. **Top issues**: Focus on REGRESSION and NEEDS_SKILL classifications from L5
3. **Concrete suggestions**: Reference specific sections of the SKILL.md that should change
4. **Next steps**: Whether to optimize, re-evaluate after manual edits, or accept the skill as-is

## Static Eval Criteria (#406)

The static eval (L3) scores the SKILL.md on these 10 dimensions (1-10 each):

### Core Criteria
| Criteria | What It Checks |
|----------|---------------|
| **Self-Contained** | All necessary context provided, no assumed external knowledge |
| **No Conflicting Information** | Instructions consistent throughout, no contradictions |
| **Security** | No hardcoded secrets, dangerous commands warned, safe defaults |
| **LLM-Navigable Structure** | Clear headings, logical flow, findable sections |
| **Actionable Instructions** | Concrete steps ("call X with Y"), not vague ("set things up") |
| **Scoped Clearly** | Explicit about what the skill does and doesn't do |

### Advanced Criteria
| Criteria | What It Checks |
|----------|---------------|
| **Tools/CLI Accuracy** | All referenced tools exist in the MCP server (deterministic) |
| **Examples Are Valid** | Code snippets parse correctly (deterministic) |
| **Error Handling Guidance** | Recovery instructions for when things fail |
| **No Hallucination Triggers** | No references to non-existent features or endpoints |

Tool accuracy and examples validity are checked deterministically (zero LLM cost). The other 8 dimensions use an LLM judge.

## Understanding WITH/WITHOUT Classifications (L5)

| Classification | WITH Skill | WITHOUT Skill | Meaning |
|---------------|------------|---------------|---------|
| **POSITIVE** | pass | fail | Skill is helping — it taught the agent something |
| **REGRESSION** | fail | pass | Skill is hurting — it confused the agent |
| **NEEDS_SKILL** | fail | fail | Both fail — skill must add this content |
| **NEUTRAL** | pass | pass | Agent already knows this — skill not needed here |

**Priority order for fixing**: REGRESSION > NEEDS_SKILL > improve POSITIVE coverage.

## Eval Config (User-Written)

The user creates these files — the framework does NOT generate test cases automatically.

### `eval/ground_truth.yaml`

```yaml
test_cases:
  - id: my_test_001
    inputs:
      prompt: "Create a dashboard showing sales by region"
    expectations:
      expected_facts:
        - "sales"
        - "region"
      expected_patterns:
        - pattern: "create_or_update_dashboard"
          min_count: 1
      assertions:
        - "The response creates a dashboard with the correct data source"
      trace_expectations:
        required_tools:
          - mcp__databricks__create_or_update_dashboard
    metadata:
      category: happy_path
```

### `eval/thinking_instructions.md`

Custom criteria for what good reasoning looks like for this specific skill.

### `eval/output_instructions.md`

Custom criteria for what correct output looks like for this specific skill.

## Common Patterns

| User says | What to run |
|-----------|-------------|
| "Check my skill" / "Is my SKILL.md good?" | `run_unit_tests` + `run_static_eval` |
| "Full evaluation" / "Test everything" | All 5 levels |
| "Why is my skill making things worse?" | `run_output_eval` — look for REGRESSION |
| "Help me set up evaluation" | `init_eval_config` + explain what to fill in |
| "Compare before and after" | `run_output_eval` on both versions, compare scores |
