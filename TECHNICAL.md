# Technical Deep Dive: Skill Evaluator Internals

This document explains how each evaluation level works, the semantic grading pipeline, scoring formulas, MCP server architecture, and MLflow integration.

For setup and usage, see [README.md](README.md). For a hands-on walkthrough, see [example.md](example.md).

---

## Table of Contents

- [Two Interfaces, One Engine](#two-interfaces-one-engine)
- [Level 1: Unit Tests](#level-1-unit-tests)
- [Level 3: Static Eval](#level-3-static-eval)
- [Level 2: Integration Tests](#level-2-integration-tests)
- [Level 4: Thinking Eval](#level-4-thinking-eval)
- [Level 5: Output Eval](#level-5-output-eval)
- [Semantic Grading Pipeline](#semantic-grading-pipeline)
- [Scoring Formulas](#scoring-formulas)
- [MCP Server Architecture](#mcp-server-architecture)
- [MLflow Integration](#mlflow-integration)
- [GEPA Optimization](#gepa-optimization)
- [Data Formats](#data-formats)

---

## Two Interfaces, One Engine

The framework has two entry points that share all underlying code:

### CLI Mode (`cli.py` → `orchestrator.py`)

The `dse evaluate` command creates an `EvaluationSuiteConfig`, passes it to `run_evaluation_suite()`, which runs levels sequentially, logs to MLflow, and generates the HTML report — all in one call.

```
dse evaluate ./my-skill --levels all
  → orchestrator.run_evaluation_suite(config)
    → UnitTestLevel().run(config)
    → StaticEvalLevel().run(config)
    → IntegrationTestLevel().run(config)
    → ThinkingEvalLevel().run(config)
    → OutputEvalLevel().run(config)
    → _generate_report()
    → _log_suite_to_mlflow()
```

### MCP Mode (`server.py` → individual tools)

The FastMCP server exposes each level as a separate tool. Claude acts as the orchestrator — calling tools one at a time, interpreting results between calls, and deciding what to run next.

```
Claude: "Evaluate this skill"
  → discover_skill(skill_dir)           # Parse SKILL.md
  → run_unit_tests(skill_dir)           # L1
  → run_static_eval(skill_dir)          # L3
  → [interpret results, show user]
  → run_output_eval(skill_dir, ...)     # L5 (if user wants)
  → generate_report(skill_dir, results) # HTML report
```

Both paths call the same `EvalLevel.run(config)` methods and produce the same `LevelResult` objects.

### State Between MCP Tool Calls

MCP tools are stateless per-call. Each tool receives `skill_dir` and reconstructs `SkillDescriptor` and `SkillTestInstructions` from disk (fast filesystem reads). Workspace config is loaded from `~/.dse/config.yaml` (saved by `authenticate_workspace` or `dse auth`).

The one piece of server-side state is the L5 **baseline cache** — WITHOUT-skill agent runs cached by prompt hash in `output_eval._baseline_cache`. Since the FastMCP server is a long-lived process, this cache persists across tool calls within the same session.

---

## Level 1: Unit Tests

**File**: `levels/unit_tests.py` (221 lines)
**No agent. No LLM. No workspace. Runs in milliseconds.**

### What it does

1. Extracts all fenced code blocks from SKILL.md and reference `.md` files using regex
2. Validates each block by language:
   - **Python**: `ast.parse()` — catches syntax errors
   - **SQL**: Balanced parentheses check + structural validation
   - **YAML**: `yaml.safe_load()` — catches parse errors
3. Checks relative links between `.md` files — catches dead `[See X](broken.md)` references
4. If `eval/tests/` exists, runs `pytest` against it via subprocess

### Output

```json
{
  "level": "unit",
  "score": 1.0,
  "feedbacks": [
    {"name": "unit/python_syntax/SKILL.md:block_3", "value": "pass", "rationale": "Valid Python syntax", "source": "CODE"},
    {"name": "unit/link/spaces.md/conversation.md", "value": "pass", "rationale": "Link to 'conversation.md' exists", "source": "CODE"}
  ],
  "metadata": {"code_blocks_tested": 30, "syntax_errors": 0}
}
```

---

## Level 3: Static Eval

**File**: `levels/static_eval.py` (373 lines)
**No agent. 1 LLM call for semantic dimensions. Deterministic checks are free.**

### The 10 Criteria (from [#406](https://github.com/databricks-solutions/ai-dev-kit/issues/406))

| # | Criteria | Type | Scale |
|---|----------|------|-------|
| 1 | Self-Contained | LLM | 1-10 |
| 2 | No Conflicting Information | LLM | 1-10 |
| 3 | Security | Hybrid (deterministic scan + LLM) | 1-10 |
| 4 | LLM-Navigable Structure | LLM | 1-10 |
| 5 | Actionable Instructions | LLM | 1-10 |
| 6 | Scoped Clearly | LLM | 1-10 |
| 7 | Tools/CLI Accuracy | Deterministic | 1-10 |
| 8 | Examples Are Valid | Deterministic | 1-10 |
| 9 | Error Handling Guidance | LLM | 1-10 |
| 10 | No Hallucination Triggers | LLM | 1-10 |

### Two-phase evaluation

**Phase 1 — Deterministic (zero LLM cost):**
- **Tool accuracy**: Cross-references every tool name found in SKILL.md against `MCPConfig.available_tools`. If the skill mentions `create_or_update_genie` but the MCP server doesn't have it, it fails.
- **Examples valid**: Extracts code blocks, runs syntax validation (same as L1).
- **Security scan**: Regex patterns for hardcoded tokens (`dapi...`, `sk-...`, `ghp_...`, `Bearer ...`, `password=...`).

**Phase 2 — LLM judge (1 batched call):**
- Sends SKILL.md content + reference files + available tools list + Phase 1 results
- The judge scores each of 8 semantic dimensions on 1-10
- Returns per-dimension score, evidence (specific quote), and recommendation

### Output format (matches #406)

```json
{
  "level": "static",
  "score": 0.75,
  "metadata": {
    "overall_score": 7.5,
    "criteria": {
      "self_contained": 8,
      "no_conflicts": 9,
      "security": 7,
      "llm_navigable": 8,
      "actionable": 7,
      "scoped_clearly": 8,
      "tool_accuracy": 6,
      "examples_valid": 10
    },
    "recommendations": [
      "Line 45 references 'dbutils.fs.cp' but doesn't explain the parameters",
      "Missing error handling for pipeline creation failures"
    ]
  }
}
```

### Scoring

- `overall_score` = average of all 10 dimension scores (1-10 scale)
- `score` (0-1 normalized) = `overall_score / 10.0`
- A dimension passes if score >= 6, fails if < 6

---

## Level 2: Integration Tests

**File**: `levels/integration_tests.py` (208 lines)
**Requires agent + Databricks workspace + MCP tools.**

### What it does

1. **MCP connectivity check**: Verifies all configured MCP servers are reachable
2. **Agent execution**: Runs `run_agent_sync_wrapper()` for each test case with the skill injected as system prompt
3. **Trace scoring**: Checks trace expectations from `ground_truth.yaml`:
   - `required_tools`: Were the expected MCP tools called?
   - `banned_tools`: Were forbidden tools avoided?
   - Tool call success rate: What percentage of tool calls succeeded?
4. **Execution success**: Did the agent produce a non-empty response?

### Agent execution flow

```
run_agent_sync_wrapper(prompt, skill_md, mcp_config)
  → ClaudeSDKClient(options)  # Claude Agent SDK
    → Streams events: tool_use, tool_result, text, system
    → Builds TraceMetrics from events
  → Returns AgentResult(response_text, events, trace_metrics)
```

The agent runs as an isolated subprocess with its own MCP server connections. The `mcp_config` parameter tells the executor which MCP servers to start (typically the Databricks MCP server).

---

## Level 4: Thinking Eval

**File**: `levels/thinking_eval.py` (294 lines)
**Requires agent + workspace + MCP tools.**

### What it does

Evaluates the **reasoning process**, not the output. Two scoring phases:

**Phase 1 — Deterministic trace scoring** (from `scorers/trace.py`):
- `required_tools(trace)`: Did the agent call the expected MCP tools?
- `banned_tools(trace)`: Did it avoid Bash when MCP was available?
- `tool_count(trace, limits)`: Within the expected call count?
- `token_budget(trace, budget)`: Didn't exhaust the context window?

**Phase 2 — LLM judge** (1 call per test case):
- Receives the full execution transcript + custom `thinking_instructions.md`
- Scores 4 dimensions (1-5 each): efficiency, clarity, recovery, completeness
- Provides evidence from the transcript for each score

### Custom thinking instructions

These are **user-written**, specific to each skill. Example for databricks-genie:

```markdown
## Efficiency
- Simple Genie Space creation should take 1-3 tool calls
- Agent should NOT use Bash to call the databricks CLI

## Recovery  
- If create_or_update_genie fails, check if the table exists first
```

The LLM judge uses these to calibrate what "good reasoning" means for this particular skill.

---

## Level 5: Output Eval

**File**: `levels/output_eval.py` (203 lines)
**Requires agent + workspace + MCP tools. The core controlled experiment.**

### WITH vs WITHOUT comparison

For each test case:
1. Run agent **WITH** the skill (SKILL.md injected as system prompt)
2. Run agent **WITHOUT** the skill (same prompt, no skill — the control)
3. Grade both responses using the semantic grader
4. Classify each assertion by comparing WITH vs WITHOUT results

### Classification labels

| Label | WITH | WITHOUT | Meaning |
|-------|------|---------|---------|
| **POSITIVE** | pass | fail | Skill taught the agent something it didn't know |
| **REGRESSION** | fail | pass | Skill confused the agent — it was better without |
| **NEEDS_SKILL** | fail | fail | Neither response handles this — skill must add content |
| **NEUTRAL** | pass | pass | Agent already knows this — skill adds no value here |

### Baseline caching

WITHOUT-skill runs are cached by prompt hash (`hashlib.sha256(prompt)`). Since the model and prompt don't change, the baseline is stable. In MCP mode, the cache persists across tool calls within the same server session.

### Fallback mode

If the full semantic grader isn't available (import error), L5 falls back to simple assertion checking — substring matching for `expected_facts` and regex for `expected_patterns`. Less precise but always works.

---

## Semantic Grading Pipeline

**File**: `grading/semantic_grader.py` (747 lines)

The grading pipeline used by L5 (and available to L4) implements a 3-phase hybrid approach:

```
Phase 1: Deterministic (zero LLM cost)
─────────────────────────────────────
expected_patterns → regex match
expected_facts    → substring match
  │
  ▼ collect failures + freeform assertions + guidelines
  
Phase 2: Agent-based grading (if transcript available)
──────────────────────────────────────────────────────
Anthropic API + transcript → per-item pass/fail + evidence
  │
  ▼ on failure, falls back to Phase 3
  
Phase 3: Semantic fallback (1 batched LLM call)
────────────────────────────────────────────────
litellm batched call → per-item pass/fail + evidence
  │
  ▼ upgrade fact failures that pass semantic check
  ▼ classify: POSITIVE / REGRESSION / NEEDS_SKILL / NEUTRAL
```

### Why 3 phases?

- **Phase 1** is free and catches exact matches instantly
- **Phase 2** can verify behavioral assertions ("agent called the correct MCP tool") by reading the execution transcript
- **Phase 3** catches semantic equivalences (e.g., `"example_questions"` matches `"sample_questions"`)

Fact failures from Phase 1 get a second chance in Phase 2/3 — if the LLM confirms the content is present using different wording, the fact is upgraded to pass.

### Model fallback chain

**File**: `grading/llm_backend.py` (262 lines)

When an LLM call fails with rate limiting (`REQUEST_LIMIT_EXCEEDED`), the backend automatically cycles through fallback models:

1. Primary model: 3 retries with exponential backoff
2. Fallback chain: GPT-5-2 → Gemini-3-1-Pro → Claude Opus 4.5 → GPT-5 → Claude Sonnet 4.6

Each fallback model gets 3 retries. If all exhausted: returns score 0.0.

---

## Scoring Formulas

### L5 Output Eval — composite score

```python
final = max(0.0, min(1.0,
    0.40 * effectiveness_delta      # pass_rate_with - pass_rate_without
  + 0.30 * pass_rate_with           # absolute quality of WITH-skill response
  + 0.15 * token_efficiency         # smaller skills get bonus (up to 1.15x)
  + 0.05 * structure                # code syntax validity
  - 0.10 * regression_rate          # penalty for REGRESSION assertions
))
```

| Weight | Dimension | Why |
|--------|-----------|-----|
| 40% | Effectiveness delta | Core question: does the skill help? |
| 30% | Pass rate with | Absolute quality matters, not just relative |
| 15% | Token efficiency | Skills consume context — conciseness is valuable |
| 5% | Structure | Syntax errors in examples teach broken patterns |
| -10% | Regression rate | Even small regressions are costly in practice |

### L3 Static Eval — overall score

```
overall_score = mean(all 10 dimension scores)    # 1-10 scale
normalized_score = overall_score / 10.0           # 0-1 for LevelResult.score
```

### Suite composite

```
composite_score = mean(all level scores)          # 0-1
```

---

## MCP Server Architecture

**File**: `server.py` (445 lines)

### FastMCP setup

```python
from fastmcp import FastMCP
mcp = FastMCP("Skill Evaluator")
```

10 tools registered via `@mcp.tool()` decorators. Each tool:
1. Parses parameters (skill_dir, optional overrides)
2. Calls `_build_level_config()` to construct `LevelConfig` from disk
3. Runs the level's `.run(config)` method
4. Returns JSON via `json.dumps(result.to_dict())`

### Async handling

Agent-based tools (L2, L4, L5) can block for minutes. They use `asyncio.to_thread()`:

```python
@mcp.tool()
async def run_thinking_eval(skill_dir, ...) -> str:
    config = _build_level_config(skill_dir, ...)
    result = await asyncio.to_thread(ThinkingEvalLevel().run, config)
    return json.dumps(result.to_dict())
```

### Error handling

Every tool wraps its body in try/except and returns structured error JSON:

```json
{"error": "No workspace config found. Call authenticate_workspace first.", "error_type": "ValueError"}
```

This lets Claude diagnose failures and suggest fixes (e.g., "You need to authenticate first").

### Nested MCP (agent-based levels)

L2/L4/L5 run a real Claude Code agent that itself needs MCP tools (the Databricks MCP server). The flow:

```
skill-evaluator MCP server
  → run_thinking_eval tool
    → run_agent_sync_wrapper(mcp_config=databricks_servers)
      → Claude Agent SDK subprocess
        → starts Databricks MCP server independently
        → agent uses Databricks tools (execute_sql, create_genie, etc.)
```

The `mcp_json_path` parameter tells the evaluator where to find the Databricks MCP server config. It auto-discovers `.mcp.json` from parent directories if not specified.

---

## MLflow Integration

### CLI mode

The orchestrator creates a single MLflow run with:
- **Tags**: `skill_name`, `eval_type`, `levels`, `framework_version`
- **Metrics**: Per-level scores (`L1/unit/score`, `L3/static/score`, etc.)
- **Artifacts**: `evaluation.json`, `report.html`

### MCP mode

MLflow logging happens in `generate_report` when results are collected. Individual level tools don't log to MLflow — they return results to Claude, who passes them to `generate_report`.

### Experiment structure

```
Experiment: /Users/you@databricks.com/GenAI/skill-evals
  Run: my-skill_eval_20260401
    Tags: skill_name=my-skill, eval_type=suite
    Metrics:
      suite/composite_score: 0.85
      L1/unit/score: 1.0
      L3/static/score: 0.75
      L3/static/self_contained: 8
      L3/static/tool_accuracy: 6
    Artifacts:
      evaluation/dse_evaluation.json
      reports/report.html
```

---

## GEPA Optimization

**Files**: `optimize/config.py`, `optimize/feedback.py`, `optimize/splitter.py`, `optimize/utils.py`

[GEPA](https://github.com/gepa-ai/gepa) (Generalized Evolutionary Prompt Architect) treats the SKILL.md as a text artifact to optimize. The modules are extracted and ready — the `run_optimization` MCP tool and `dse optimize` CLI command are scaffolded.

### Presets

| Preset | Max Metric Calls | Typical Time |
|--------|-----------------|-------------|
| `minimal` | 3 | ~2 min |
| `quick` | 5 (× components) | ~10 min |
| `standard` | 5 (× components, minibatch) | ~30 min |
| `thorough` | 5 (× components, detailed) | ~90 min |

### Optimization loop

```
seed = original SKILL.md
  → GEPA reflect: read side_info (failed assertions, classifications, human feedback)
  → GEPA mutate: propose targeted change
  → Evaluator: run WITH/WITHOUT comparison on mutated SKILL.md
  → GEPA select: keep if improved (Pareto frontier)
  → repeat until convergence or max iterations
```

### Human feedback flow

```
HTML report → user reviews → clicks "Save Feedback" → feedback.json
  → dse optimize --feedback feedback.json
    → feedback.load_feedback() → feedback.feedback_to_gepa_background()
      → injected into GEPA's reflection context
```

---

## Data Formats

### `eval/ground_truth.yaml`

```yaml
metadata:
  skill_name: my-skill
  version: 0.1.0

test_cases:
  - id: test_001
    inputs:
      prompt: "User's request to the agent"
    expectations:
      expected_facts:                  # Substring matches (deterministic)
        - "expected text"
      expected_patterns:               # Regex matches (deterministic)
        - pattern: "tool_name"
          min_count: 1
      assertions:                      # Freeform (LLM-evaluated)
        - "The response does X correctly"
      guidelines:                      # Quality guidelines (LLM-evaluated)
        - "Agent should use MCP tools over Bash"
      trace_expectations:              # Checked against execution trace
        required_tools: [mcp__databricks__tool]
        banned_tools: [Bash]
        tool_limits: {Bash: 3}
        token_budget: {max_total: 100000}
    metadata:
      category: happy_path             # For stratified splitting
      difficulty: easy
```

### `LevelResult` (returned by all levels and MCP tools)

```json
{
  "level": "static",
  "score": 0.75,
  "passed": true,
  "num_feedbacks": 18,
  "feedbacks": [
    {
      "name": "static/self_contained",
      "value": "pass",
      "rationale": "Score: 8/10. All APIs documented with parameters.",
      "source": "LLM_JUDGE"
    }
  ],
  "metadata": {
    "overall_score": 7.5,
    "criteria": {"self_contained": 8, "no_conflicts": 9, ...},
    "recommendations": ["Add error handling for X"]
  }
}
```

### `~/.dse/config.yaml` (saved workspace config)

```yaml
default_profile: e2-demo-field-eng
profiles:
  e2-demo-field-eng:
    profile: e2-demo-field-eng
    host: https://e2-demo-field-eng.cloud.databricks.com
    catalog: ac_demo
    schema: dc_assistant
    warehouse_id: 01370556fad60fda
    experiment_path: /Users/you@databricks.com/GenAI/skill-evals
```
