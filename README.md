# databricks-skill-evaluator

Evaluate and optimize [Claude Code skills](https://docs.anthropic.com/en/docs/agents-and-tools/agent-skills) against real Databricks workspaces. Point it at any skill directory and get comprehensive evaluation across 5 levels, with results logged to Databricks MLflow.

## Why

Claude Code skills are markdown files that teach the agent domain-specific knowledge. But how do you know if a skill actually helps? Does the agent produce better results with it than without it? Are the code examples correct? Do the tool references point at real tools?

This framework answers those questions with a 5-level testing pyramid:

| Level | Name | What It Tests | Needs Agent? |
|-------|------|---------------|-------------|
| L1 | **Unit Tests** | Code block syntax, broken links, YAML validity | No |
| L2 | **Integration Tests** | End-to-end workflows against real Databricks | Yes |
| L3 | **Static Eval** | SKILL.md quality (10 criteria via LLM judge) | No |
| L4 | **Thinking Eval** | Agent reasoning: efficiency, clarity, recovery | Yes |
| L5 | **Output Eval** | WITH vs WITHOUT skill comparison | Yes |

L1 and L3 run in seconds with zero agent cost. L2/L4/L5 run real Claude Code agents and compare behavior with and without the skill.

## Install

```bash
pip install databricks-skill-evaluator
```

## Quick Start

```bash
# 1. Authenticate with your Databricks workspace
dse auth --profile my-workspace --catalog main --schema skill_test

# 2. Initialize eval config for your skill (creates templates with TODOs)
dse init ./my-skill

# 3. *** YOU WRITE YOUR TEST CASES ***
#    Edit eval/ground_truth.yaml — prompts, expected facts, assertions
#    Edit eval/thinking_instructions.md — what good reasoning looks like
#    Edit eval/output_instructions.md — what correct output looks like

# 4. Run evaluation
dse evaluate ./my-skill --levels unit,static              # Quick (seconds)
dse evaluate ./my-skill --levels all --mcp-json .mcp.json  # Full (minutes)

# 5. Review report.html, export feedback.json

# 6. Optimize based on results
dse optimize ./my-skill --feedback eval/feedback.json --preset quick
```

## What You Provide

The framework provides structure, scoring, and tooling. **You** provide the skill and write the evaluation criteria. No test cases are auto-generated — you define what "good" looks like for your specific skill.

### 1. Your skill directory

A directory with a `SKILL.md` (and optional reference files):

```
my-skill/
  SKILL.md              # Required: frontmatter (name, description) + instructions
  reference.md          # Optional: additional reference files
```

The `SKILL.md` must have YAML frontmatter:

```yaml
---
name: my-skill
description: "What this skill does and when to use it"
---
```

### 2. Your evaluation config (created by `dse init`, written by you)

```
my-skill/
  eval/                 # Created by `dse init` with TODO templates
    ground_truth.yaml   # YOU write: test cases with prompts, expected facts, assertions
    manifest.yaml       # YOU configure: scorers, quality gates, trace expectations
    thinking_instructions.md  # YOU write: what good reasoning looks like for your skill
    output_instructions.md    # YOU write: what correct output looks like
    source_of_truth/          # YOU add: expected output files for comparison
```

`dse init` creates these files with placeholder TODOs. You fill them in based on your domain expertise. See [example.md](example.md) for a complete walkthrough of writing eval criteria for a real skill.

## Evaluation Levels

### L1: Unit Tests

Extracts every fenced code block from the skill's markdown files and validates syntax:
- Python blocks parsed with `ast.parse()`
- SQL blocks checked for balanced parentheses and structure
- YAML blocks validated with `yaml.safe_load()`
- Relative links between `.md` files verified

Zero LLM cost. Catches broken examples before they confuse the agent.

### L2: Integration Tests

Runs the real Claude Code agent against your Databricks workspace:
- Tests MCP tool connectivity
- Executes test cases from `ground_truth.yaml`
- Validates tool call success rates
- Checks trace expectations (required tools, banned tools, call limits)

### L3: Static Eval

An LLM judge evaluates the SKILL.md document itself across 10 quality dimensions:

1. Self-contained
2. No conflicting information
3. Security (no hardcoded secrets)
4. LLM-navigable structure
5. Actionable instructions
6. Scoped clearly
7. Tool/CLI accuracy (deterministic: cross-references MCP tools)
8. Examples valid (deterministic: syntax checks)
9. Error handling guidance
10. No hallucination triggers

Deterministic checks run first at zero cost. Semantic dimensions use 1 batched LLM call.

### L4: Thinking Eval

Evaluates HOW the agent reasons, not what it produces:
- **Efficiency**: Did it use minimum necessary tool calls?
- **Clarity**: Did it show confusion or backtracking?
- **Recovery**: How did it handle errors?
- **Completeness**: Did it finish all required steps?

Uses custom `thinking_instructions.md` that you write for your skill's specific workflows.

### L5: Output Eval

The core controlled experiment. For each test case:
1. Run agent **WITH** the skill
2. Run agent **WITHOUT** the skill (cached baseline)
3. Grade both responses with the semantic grader
4. Classify each assertion: POSITIVE / REGRESSION / NEEDS_SKILL / NEUTRAL

Score formula: 40% effectiveness delta + 30% pass rate + 15% token efficiency + 5% structure - 10% regression penalty.

## MLflow Integration

All results are logged to Databricks MLflow:

```
Experiment: /Users/you@databricks.com/GenAI/skill-evals
  Run: my-skill_eval_20260401
    Tags: skill_name, eval_type, levels, framework_version
    Metrics: composite_score, per-level scores, per-dimension scores
    Artifacts: evaluation.json, report.html
```

Compare runs across branches, track quality over time, and drill into per-task metrics directly in the Databricks workspace UI.

## HTML Report

Every evaluation generates a self-contained HTML report at `eval/report.html`:
- Summary dashboard with composite score
- Per-level cards showing every check with pass/fail
- Feedback form with export to `feedback.json` for optimization

## Optimization

Uses [GEPA](https://github.com/gepa-ai/gepa) (Generalized Evolutionary Prompt Architect) to iteratively improve the SKILL.md:

```bash
dse optimize ./my-skill --feedback eval/feedback.json --preset quick --apply
```

| Preset | Iterations | Time |
|--------|-----------|------|
| `minimal` | ~3 | ~2 min |
| `quick` | ~15 | ~10 min |
| `standard` | ~50 | ~30 min |
| `thorough` | ~150 | ~90 min |

The optimizer reads human feedback, runs mutations, evaluates each candidate with the WITH/WITHOUT comparison, and selects the best from a Pareto frontier.

## CLI Reference

```
dse auth       Authenticate with Databricks and save config
dse init       Initialize eval/ config for a skill directory
dse evaluate   Run evaluation (--levels unit,static,integration,thinking,output,all)
dse optimize   Run GEPA optimization (--preset minimal|quick|standard|thorough)
```

Key flags for `dse evaluate`:

| Flag | Purpose |
|------|---------|
| `--levels` | Comma-separated levels to run (default: `unit,static`) |
| `--mcp-json` | Path to `.mcp.json` for MCP tool access |
| `--profile` | Databricks config profile |
| `--experiment` | MLflow experiment path |
| `--agent-model` | Claude model override |
| `--suggest-improvements` | Generate actionable improvement suggestions |
| `--compare-baseline` | MLflow run ID to compare against |

## Architecture

```
  dse auth | init | evaluate | optimize
          │
          ▼
    Orchestrator
    (runs levels sequentially)
          │
          ▼
  ┌─────────────────────────────────┐
  │       5 Evaluation Levels       │
  │                                 │
  │  L1 Unit ─── syntax, links     │
  │  L2 Integration ── agent + ws  │
  │  L3 Static ── LLM judge        │
  │  L4 Thinking ── trace quality  │
  │  L5 Output ── WITH/WITHOUT     │
  └──────────────┬──────────────────┘
                 │
  ┌──────────────▼──────────────────┐
  │     Shared Infrastructure       │
  │                                 │
  │  Semantic Grader (3-phase)      │
  │  Claude Agent SDK (executor.py) │
  │  Deterministic Scorers          │
  │  LLM Backend (model fallback)   │
  │  MLflow Tracing + Assessments   │
  │  HTML Report Generator          │
  └─────────────────────────────────┘
```

## Project Structure

```
databricks-skill-evaluator/
  SKILL.md                     # Claude Code skill (teaches evaluation workflow)
  pyproject.toml               # pip install, dse entry point
  README.md                    # This file
  TECHNICAL.md                 # Deep dive into internals
  example.md                   # End-to-end walkthrough with databricks-genie
  src/skill_evaluator/
    cli.py                     # Click CLI — dse command
    orchestrator.py            # Suite runner
    auth.py                    # Databricks auth + ~/.dse/config.yaml
    skill_discovery.py         # Parse SKILL.md frontmatter + references
    mcp_resolver.py            # Resolve .mcp.json for agent execution
    test_instructions.py       # Load eval/ config (ground_truth, instructions)
    core/
      config.py                # EvaluatorConfig, QualityGates, MLflowConfig
      dataset.py               # EvalRecord, YAMLDatasetSource
      trace_models.py          # TraceMetrics, ToolCall, FileOperation
    levels/
      base.py                  # EvalLevel ABC, LevelConfig, LevelResult
      unit_tests.py            # L1: code block syntax validation
      integration_tests.py     # L2: real agent + Databricks workspace
      static_eval.py           # L3: LLM judge, 10 criteria, 1-10 scale
      thinking_eval.py         # L4: agent reasoning quality
      output_eval.py           # L5: WITH/WITHOUT comparison
      agent_evaluator.py       # Agent execution wrapper for GEPA
    grading/
      semantic_grader.py       # 3-phase grading (deterministic → agent → LLM)
      llm_backend.py           # completion_with_fallback, model fallback chain
    scorers/
      deterministic.py         # python_syntax, sql_syntax, pattern_adherence
      trace.py                 # tool_count, required_tools, banned_tools
      llm_judges.py            # LLM-based dynamic scorers
    optimize/
      config.py                # GEPA presets (minimal/quick/standard/thorough)
      feedback.py              # Human feedback → GEPA background
      splitter.py              # Train/val dataset splitting
      utils.py                 # Token counting, path resolution
    reporting/
      html_report.py           # Self-contained HTML report generator
    criteria/
      eval_criteria.py         # SkillSet/Skill parsing for eval rubrics
      builtin/                 # Shipped evaluation criteria
        general-quality/       # Response quality rubric
        sql-correctness/       # SQL best practices rubric
        tool-selection/        # MCP tool preference rubric
```

## Full Walkthrough

See [example.md](example.md) for a complete step-by-step walkthrough using the `databricks-genie` skill.

See [TECHNICAL.md](TECHNICAL.md) for implementation details — how each level works internally, the semantic grading pipeline, scoring formulas, and MLflow integration.

## License

MIT
