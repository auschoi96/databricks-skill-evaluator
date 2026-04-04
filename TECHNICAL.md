# Technical Deep Dive: Skill Evaluator Internals

This document explains how each evaluation level works, the semantic grading pipeline, scoring formulas, and MLflow integration.

For setup and usage, see [README.md](README.md). For a hands-on walkthrough, see [example.md](example.md).

---

## Table of Contents

- [What Are These Tests?](#what-are-these-tests)
- [How CLI Evaluation Works](#how-cli-evaluation-works)
- [Level 1: Unit Tests](#level-1-unit-tests)
- [Level 2: Integration Tests](#level-2-integration-tests)
- [Level 3: Static Eval](#level-3-static-eval)
- [Level 4: Thinking Eval](#level-4-thinking-eval)
- [Level 5: Output Eval](#level-5-output-eval)
- [Semantic Grading Pipeline](#semantic-grading-pipeline)
- [Scoring Formulas](#scoring-formulas)
- [Agent MCP Configuration](#agent-mcp-configuration-l2-l4-l5)
- [MLflow Integration](#mlflow-integration)
- [GEPA Optimization](#gepa-optimization)
- [Data Formats](#data-formats)

---

## What Are These Tests?

The evaluator tests a **Claude Code skill** (a SKILL.md document and its reference files) across 5 levels. The key conceptual distinction is **where the tests come from** and **what they're actually testing**:

### The Two Categories

**Levels 1 and 3 — Auditing the skill document itself (no agent execution)**

These levels treat the SKILL.md as a static artifact and check its quality. The evaluator itself generates all the checks — you don't write test cases for these. They answer: *"Is this skill document well-formed, accurate, and high-quality?"*

- **L1 (Unit Tests)**: The evaluator parses every fenced code block out of SKILL.md and its reference `.md` files, then validates syntax — Python via `ast.parse()`, YAML via `yaml.safe_load()`, SQL via structural checks. It also verifies that relative markdown links point to files that exist. Optionally, if you put pytest tests in `eval/tests/`, it runs those too.
- **L3 (Static Eval)**: An LLM judge reads the SKILL.md and scores it on 10 quality dimensions (is it self-contained? are instructions actionable? does it reference real tools?). Deterministic checks run first for free (hardcoded secrets scan, tool name cross-referencing against the MCP server).

**Levels 2, 4, and 5 — Testing the skill in action (real agent execution)**

These levels spin up a real Claude agent with the SKILL.md injected as system prompt, run it against a real Databricks workspace, and evaluate what happens. You define test cases in `ground_truth.yaml` — each test case is a prompt (a user request) plus expectations about what should happen. They answer: *"Does this skill actually make the agent better at the task?"*

- **L2 (Integration Tests)**: Run the agent with each test prompt. Check: did it complete? Did it call the right MCP tools? Did it avoid banned tools? What was the tool success rate?
- **L4 (Thinking Eval)**: Run the agent and evaluate its *reasoning process* — was it efficient? Did it recover from errors? Did it show clear understanding? Scored by an LLM judge reading the execution transcript.
- **L5 (Output Eval)**: The controlled experiment. Run the agent twice — once WITH the skill, once WITHOUT — and compare. Also verify that resources were actually created in Databricks (not just mentioned in the response text), and compare against source-of-truth expected outputs.

### Where Each Level Gets Its Tests

| Level | Test Source | Who Writes It | What Gets Tested |
|-------|-----------|---------------|-----------------|
| L1 | Built-in + optional `eval/tests/` | Evaluator + optionally you | Code block syntax, MCP tool availability, markdown links |
| L2 | `eval/ground_truth.yaml` test cases | You | Agent completes tasks and uses correct tools |
| L3 | Built-in 10-dimension rubric | Evaluator | SKILL.md document quality |
| L4 | `ground_truth.yaml` + `thinking_instructions.md` | You | Agent's reasoning efficiency and clarity |
| L5 | `ground_truth.yaml` + `source_of_truth/` files | You | Agent output quality, created assets, WITH vs WITHOUT |

### What You Provide vs What the Evaluator Generates

**You provide** (in your skill's `eval/` directory):
- `ground_truth.yaml` — Test cases: prompts + expectations (used by L2, L4, L5)
- `thinking_instructions.md` — Custom reasoning criteria for L4 (optional)
- `output_instructions.md` — Custom output criteria for L5 (optional)
- `source_of_truth/` files — Expected outputs for L5 comparison (optional)
- `tests/` directory — Custom pytest tests for L1 (optional)

**The evaluator generates** (built-in, no configuration needed):
- L1 syntax validation of all code blocks
- L1 MCP tool availability checking (AST-parsed from MCP server source)
- L1 markdown link checking
- L3 security scanning (regex for hardcoded tokens)
- L3 tool accuracy checking (cross-reference against MCP server)
- L3 LLM quality rubric (10 dimensions)

### Quick Reference: Requirements Per Level

| Level | Agent? | LLM? | Workspace? | Runtime |
|-------|--------|------|-----------|---------|
| L1 Unit | No | No | No | Milliseconds |
| L2 Integration | Yes | No | Yes | Minutes |
| L3 Static | No | 1 call | No | Seconds |
| L4 Thinking | Yes | 1 per test | Yes | Minutes |
| L5 Output | Yes | 2-3 per test | Yes | Minutes-hours |

---

## How CLI Evaluation Works

`cli.py` → `orchestrator.py`

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

Each level's `.run(config)` method returns a `LevelResult` with score, feedbacks, and metadata. Workspace config is loaded from `~/.dse/config.yaml` (saved by `dse auth`).

---

## Level 1: Unit Tests

**File**: `levels/unit_tests.py` (221 lines)
**No agent. No LLM. No workspace. Runs in milliseconds.**

L1 is a static audit of the skill document. It does NOT run a real agent — it treats SKILL.md and its reference files as text and validates the code examples and links within them.

### What it does — step by step

**Step 1: Extract all code blocks from the skill's markdown files.**

The evaluator reads SKILL.md plus every reference `.md` file the skill includes. For each file, it uses a regex to extract every fenced code block and its declared language:

```
```python            ← language tag
import os            ← code content
print("hello")
```                  ← end fence
```

This happens in `_extract_code_blocks()` which uses the regex `` ```(\w*)\n(.*?)``` `` with `re.DOTALL` to capture multi-line blocks.

**Step 2: Validate each code block by language.**

For each extracted block, the evaluator runs a language-specific syntax check:

- **Python blocks** → `ast.parse(code)`. This is the same parser the Python interpreter uses. It catches syntax errors (missing colons, unmatched brackets, invalid indentation) but doesn't execute the code. A block like `print("hello"` would fail with `SyntaxError at line 1: '(' was never closed`.

- **SQL blocks** → Balanced parentheses check. Counts `(` and `)` through the entire block, ensuring depth never goes negative and ends at zero. Also rejects empty blocks. This catches the most common SQL typos (missing closing parens in nested queries) without needing a full SQL parser.

- **YAML blocks** → `yaml.safe_load(code)`. Uses PyYAML's safe loader to verify the YAML parses without errors. Catches issues like incorrect indentation, missing colons, or invalid characters.

Each check produces a feedback entry like:
```json
{"name": "unit/python_syntax/SKILL.md:block_3", "value": "pass", "rationale": "Valid Python syntax"}
```

**Step 3: Check that referenced MCP tools actually exist.**

The evaluator extracts all MCP tool references from the skill content — both explicit `mcp__databricks__tool_name` patterns and bare tool names that match known MCP tools. It then cross-references them against the actual tools available in the MCP server.

The tool list is populated by statically parsing the MCP server's Python source code via AST (following `.mcp.json` → entry point → `server.py` → tool modules → `@mcp.tool` decorated functions). This is purely file reads + `ast.parse()` — no subprocess, no MCP connections.

If MCP config isn't available (no `.mcp.json` found), the check is gracefully skipped.

**Step 4: Check for broken relative links.**

The evaluator scans all markdown files for link patterns `[text](target)` using regex. For each relative link (skipping `http://`, `https://`, `#`, and `mailto:` URLs), it checks whether the target file actually exists on disk relative to the skill directory. This catches dead references like `[See the API reference](api.md)` when `api.md` doesn't exist.

**Step 5: Run user-provided pytest tests (optional).**

If the skill has an `eval/tests/` directory, the evaluator runs `pytest` against it via `subprocess.run()` with a 120-second timeout. This is the only part of L1 where **you** write the tests. The evaluator captures stdout/stderr and reports pass/fail:

```json
{"name": "unit/pytest/suite", "value": "pass", "rationale": "5 passed in 0.3s"}
```

### Scoring

```
score = passed_checks / total_checks
```

All checks (syntax validations + link checks + pytest results) are weighted equally. A skill with 30 valid code blocks and 1 syntax error scores `30/31 = 0.97`. Pass threshold: `score >= 0.5`.

### Output

```json
{
  "level": "unit",
  "score": 0.95,
  "feedbacks": [
    {"name": "unit/python_syntax/SKILL.md:block_3", "value": "pass", "rationale": "Valid Python syntax", "source": "CODE"},
    {"name": "unit/sql_syntax/SKILL.md:block_7", "value": "pass", "rationale": "Valid SQL syntax", "source": "CODE"},
    {"name": "unit/yaml_syntax/config.md:block_1", "value": "pass", "rationale": "Valid YAML syntax", "source": "CODE"},
    {"name": "unit/tool_available/create_or_update_genie", "value": "pass", "rationale": "Tool 'create_or_update_genie' found in MCP server", "source": "CODE"},
    {"name": "unit/tool_available/ask_genie", "value": "pass", "rationale": "Tool 'ask_genie' found in MCP server", "source": "CODE"},
    {"name": "unit/link/SKILL.md/See spaces.md", "value": "pass", "rationale": "Link to 'spaces.md' exists", "source": "CODE"},
    {"name": "unit/pytest/suite", "value": "pass", "rationale": "8 passed in 1.2s", "source": "CODE"}
  ],
  "metadata": {"code_blocks_tested": 30, "syntax_errors": 0}
}
```

### Why this matters

If a skill teaches Claude to write Python like `from databricks.sdk import WorkspaceClient(`, that syntax error gets baked into Claude's context window and can cause it to generate broken code. Similarly, if a skill references `create_or_update_genie` but that tool doesn't exist in the MCP server, every agent-based level (L2, L4, L5) will waste minutes running an agent that can't succeed. L1 catches both problems in milliseconds before any expensive agent runs happen.

---

## Level 2: Integration Tests

**File**: `levels/integration_tests.py` (208 lines)
**Requires agent + Databricks workspace + MCP tools.**

L2 is NOT running test files from the project. It takes test cases you defined in `ground_truth.yaml`, spins up a **real Claude agent** with the SKILL.md injected, runs it against a **real Databricks workspace** via MCP tools, and checks whether the agent succeeded.

### What it does — step by step

**Step 1: Verify MCP connectivity.**

Before running any agent, L2 checks that all configured MCP servers (defined in `.mcp.json`) are reachable. If the Databricks MCP server isn't configured, the entire level is skipped with score 0.0. This prevents wasting minutes on agent runs that will fail immediately.

**Step 2: Load test cases from `ground_truth.yaml`.**

Each test case has a prompt (what the user asks the agent to do) and expectations:

```yaml
test_cases:
  - id: create_simple_space
    inputs:
      prompt: "Create a Genie Space called 'Sales Analytics' using the table ac_demo.dc_assistant.customers"
    expectations:
      trace_expectations:
        required_tools: [mcp__databricks__create_or_update_genie]
        banned_tools: [Bash]
```

If any test cases have `category: integration` in their metadata, only those are used. Otherwise all test cases are run.

**Step 3: Run a real Claude agent for each test case.**

For each test case, the evaluator calls `run_agent_sync_wrapper()`:

```
run_agent_sync_wrapper(prompt, skill_md, mcp_config)
  → Claude Agent SDK creates a new agent session
    → SKILL.md injected as system prompt
    → MCP servers started (Databricks tools available)
    → Agent streams events: tool_use, tool_result, text
    → Events captured and assembled into TraceMetrics
  → Returns AgentResult(response_text, events, trace_metrics)
```

The agent runs as a real Claude Code instance — it can call MCP tools, read files, write code. The `mcp_config` parameter tells it which MCP servers to connect to (typically the Databricks MCP server, providing tools like `create_or_update_genie`, `execute_sql`, etc.).

**Step 4: Check execution success.**

The simplest check: did the agent produce a response longer than 10 characters? An empty or near-empty response means the agent failed to engage with the task.

**Step 5: Check trace expectations.**

The evaluator inspects the agent's execution trace (the record of every tool call it made) against the expectations in `ground_truth.yaml`:

- **`required_tools`**: Did the agent call the expected MCP tools? If you expect the agent to call `create_or_update_genie`, it must appear in the trace. This verifies the skill is actually guiding the agent to use the right tools.

- **`banned_tools`**: Did the agent avoid forbidden tools? Typically `Bash` is banned when an MCP tool exists for the job. If the skill should teach the agent to use `create_or_update_genie` instead of shelling out to `databricks genie create`, this catches regressions.

- **`tool_limits`**: Did the agent stay within the call count limits? If `ground_truth.yaml` specifies `tool_limits: {Bash: 3}`, the evaluator verifies `Bash` was called at most 3 times. This catches skills that allow excessive tool usage (e.g., too many shell commands when MCP tools should be preferred).

- **Tool success rate**: What percentage of tool calls returned success? Computed as `(total - failed) / total`. Must be >= 80%. If the agent is calling tools that consistently error, the skill may have incorrect instructions.

### Scoring

```
score = successful_tests / total_tests
```

A test case is "successful" if the agent produced a non-empty response. Individual trace expectation checks are recorded as feedbacks but don't directly affect the binary success/fail per test case.

### Output

```json
{
  "level": "integration",
  "score": 0.8,
  "feedbacks": [
    {"name": "integration/mcp_connectivity/databricks", "value": "pass", "source": "CODE"},
    {"name": "integration/create_simple_space/execution", "value": "pass",
     "rationale": "Agent completed in 12.3s", "source": "CODE"},
    {"name": "integration/create_simple_space/required_tool/mcp__databricks__create_or_update_genie",
     "value": "pass", "rationale": "Required tool 'create_or_update_genie' used", "source": "CODE"},
    {"name": "integration/create_simple_space/banned_tool/Bash",
     "value": "pass", "rationale": "Banned tool 'Bash' NOT used", "source": "CODE"},
    {"name": "integration/create_simple_space/tool_success_rate",
     "value": "pass", "rationale": "Tool success rate: 100% (3/3)", "source": "CODE"}
  ],
  "task_results": [
    {"task_id": "create_simple_space", "execution_time_s": 12.3, "success": true, "tool_calls": 3}
  ],
  "metadata": {"num_integration_tests": 5, "success_rate": 0.8}
}
```

### Why this matters

L1 and L3 can tell you the skill document is well-written. L2 tells you it actually **works** — the agent can take the skill's instructions, connect to a real Databricks workspace, and execute the task. It's the first level where rubber meets road.

---

## Level 3: Static Eval

**File**: `levels/static_eval.py`
**No agent. 1 LLM call for semantic dimensions. Deterministic checks are free.**

L3 is a quality audit of the SKILL.md document. Like L1, it does not run an agent. Unlike L1 (which only checks syntax), L3 evaluates the document's content and structure — is it self-contained? Are the instructions actionable? Could it cause hallucinations?

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

### Deduplication with L1

Criteria 7 (tool accuracy) and 8 (examples valid) overlap with L1's syntax and tool checks. To avoid running the same validation twice, L3 receives L1's results via `LevelConfig.prior_results` and derives scores from L1's feedbacks. No checks are re-run.

The shared validation functions (`extract_code_blocks`, `check_python_syntax`, `check_sql_syntax`, `check_yaml_syntax`) live in `levels/shared_validators.py` and are imported by both L1 and L3.

### How it works — two-phase evaluation

**Phase 1 — Deterministic (zero LLM cost):**

Three checks run before any LLM is invoked:

- **Tool accuracy**: Cross-references MCP tool names in SKILL.md against `MCPConfig.available_tools`. Handles both bare names (`create_or_update_genie`) and prefixed names (`mcp__databricks__create_or_update_genie`). Score: `(found_tools / referenced_tools) * 10`. When L1 has already run, the score is derived from L1's `unit/tool_available/` feedbacks.

- **Examples valid**: Validates Python, SQL, and YAML code blocks via `shared_validators`. When L1 has already run, the score is derived from L1's `unit/python_syntax/`, `unit/sql_syntax/`, and `unit/yaml_syntax/` feedbacks.

- **Security scan** (`_check_security_deterministic`): Regex patterns scan the entire skill content for:
  - `dapi[a-f0-9]{32,}` — Databricks API tokens
  - `sk-[a-zA-Z0-9]{32,}` — OpenAI-style API keys
  - `ghp_[a-zA-Z0-9]{36,}` — GitHub personal access tokens
  - `Bearer\s+[a-zA-Z0-9\-_.]{20,}` — Bearer tokens
  - `password\s*=\s*['"][^'"]{8,}['"]` — Hardcoded passwords

**Phase 2 — LLM judge (1 batched call):**

A single LLM call evaluates the remaining 8 semantic dimensions. The prompt includes:
- The full SKILL.md content
- All reference files (truncated to 2000 chars each)
- The list of available MCP tools
- Phase 1 results (so the LLM knows what already passed/failed)

The judge returns a JSON array scoring each dimension 1-10 with specific evidence (quotes from the skill) and actionable recommendations for any score below 7.

**LLM unavailability**: If the LLM backend is not installed or the judge call fails, L3 returns explicit `"skip"` feedback entries for each LLM dimension and sets `metadata.llm_skipped = true`. The coverage-factor scoring (below) ensures the overall score reflects that most of the evaluation was skipped.

### Scoring

```
overall_score = mean(all evaluated dimension scores)  # 1-10 scale
coverage_factor = dimensions_evaluated / 10           # penalizes missing dims
normalized_score = (overall_score / 10.0) * coverage_factor  # 0-1 for LevelResult.score
```

A dimension **passes** at score >= 6, **fails** below 6.

When only deterministic dimensions run (LLM unavailable), coverage_factor = 2/10 = 0.2, capping the max score at 0.2 to prevent inflation.

### Output format

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
      "examples_valid": 10,
      "error_handling": 7,
      "no_hallucination_triggers": 6
    },
    "recommendations": [
      "Line 45 references 'dbutils.fs.cp' but doesn't explain the parameters",
      "Missing error handling for pipeline creation failures"
    ],
    "dimensions_evaluated": 10,
    "dimensions_total": 10,
    "coverage_factor": 1.0
  }
}
```

### Why this matters

A skill can have perfect syntax (L1 passes) but be structurally terrible — vague instructions, contradictory guidance, references to non-existent tools. L3 catches these higher-level quality issues before spending money on agent runs.

---

## Level 4: Thinking Eval

**File**: `levels/thinking_eval.py`
**Requires agent + workspace + MCP tools.**

L4 evaluates **how the agent reasons**, not what it outputs. It runs the same real agent as L2, but instead of just checking "did it succeed?", it builds a comprehensive transcript of the agent's execution and has an LLM judge assess reasoning quality.

### How L4 differs from L2 and L5

| Aspect | L2 (Integration) | L4 (Thinking) | L5 (Output) |
|--------|------------------|---------------|-------------|
| **Question answered** | Did the agent succeed? | How well did it reason? | Is the skill helping? |
| **Agent runs** | 1 (WITH skill) | 1 (WITH skill) | 2 (WITH + WITHOUT) |
| **Trace checks** | required/banned tools, success rate | required/banned tools, token budget | asset verification, SoT comparison |
| **LLM judge** | None | Reasoning quality (4 dimensions) | Response quality (WITH vs WITHOUT) |
| **Scoring signal** | Binary pass/fail per test | Continuous 1-5 per dimension | Assertion classification (POSITIVE/REGRESSION) |

L4's unique value: it's the only level that feeds the full execution transcript to an LLM judge for reasoning quality assessment. L2 checks "did it work?" and L5 checks "what did it produce?" — L4 checks "how did it think?"

### What it does — step by step

**Step 1: Run agent with skill.**

Same as L2 — `run_agent_sync_wrapper(prompt, skill_md, mcp_config)` produces an `AgentResult` with the full event stream. MLflow autolog (`mlflow.anthropic.autolog()`) automatically captures the complete session as an MLflow trace for observability.

**Step 2: Deterministic trace scoring.**

Shared checks (via `shared_validators.check_trace_expectations()`, same code as L2):

- **`required_tools`**: Did the agent call the expected MCP tools?
- **`banned_tools`**: Did it avoid forbidden tools?
- **`tool_limits`**: Were specific tools called within count limits?

L4-only check:

- **`token_budget`**: Total token usage didn't exceed a budget. Example: `{max_total: 100000}`. Catches agents that burn through context window with verbose reasoning.

**Step 3: Build comprehensive transcript.**

The `_build_comprehensive_transcript()` function processes the full event stream from `AgentResult.events` into a structured, human-readable format:

```
========================================
TURN 1  (tokens: input=1200 output=340)
========================================

[THINKING] The user wants to create a Genie Space for Sales Analytics.
I'll use create_or_update_genie with the specified table.

[TOOL_USE] mcp__databricks__create_or_update_genie
  Input: {"name": "Sales Analytics", "tables": ["ac_demo.dc_assistant.customers"]}

[TOOL_RESULT] SUCCESS (mcp__databricks__create_or_update_genie)
  {"space_id": "01f12ea7...", "name": "Sales Analytics", "tables_count": 1}

========================================
TURN 2  (tokens: input=2400 output=180)
========================================

[THINKING] The space was created. Let me verify it.

[TOOL_USE] mcp__databricks__get_genie_space
  Input: {"space_id": "01f12ea7..."}

[TOOL_RESULT] SUCCESS (mcp__databricks__get_genie_space)
  {"space_id": "01f12ea7...", ...}
```

Key design decisions:
- **Agent reasoning text (`[THINKING]`)** is included — these are the most important signal for clarity and completeness scoring. Earlier versions dropped text blocks entirely.
- **Tool_use → tool_result pairs** are structured together — the judge can see what was called and what happened.
- **Errors are marked with `[ERROR]`** — critical for recovery scoring.
- **Per-turn token usage** — directly supports efficiency assessment.
- **Budget**: Up to 15,000 characters. If exceeded, the transcript preserves the beginning (initial approach) and end (completion), trimming the middle.

**Step 4: LLM judge for reasoning quality (1 call per test case).**

The LLM judge receives the comprehensive transcript, trace summary, and custom `thinking_instructions.md`. It scores 4 dimensions on a 1-5 scale:

| Dimension | What it measures |
|-----------|-----------------|
| **Efficiency** | Did the agent use minimum necessary tool calls? Avoid redundant reads, unnecessary retries, roundabout approaches? |
| **Clarity** | Did the agent's reasoning show clear task understanding? No confusion, backtracking, or misinterpretation? |
| **Recovery** | When errors occurred, did the agent diagnose and try alternatives? Or did it loop on the same failed approach? |
| **Completeness** | Did the agent complete all required steps? Were any critical actions skipped? |

Each dimension gets a score, evidence quote from the transcript, and passes at >= 3.

### Custom thinking instructions

You write `eval/thinking_instructions.md` to define what "good reasoning" means for your specific skill:

```markdown
## Efficiency
- Simple Genie Space creation should take 1-3 tool calls
- Agent should NOT use Bash to call the databricks CLI

## Recovery
- If create_or_update_genie fails, check if the table exists first
```

The LLM judge uses these to calibrate its scoring — what counts as "efficient" for a Genie skill (1-3 calls) is different from what's efficient for a complex pipeline skill (10-15 calls).

### MLflow tracing

Every L4 agent run is automatically traced via `mlflow.anthropic.autolog()`. The trace captures the complete conversation — prompts, tool calls, results, token usage, timing — and is stored in MLflow. Users can inspect the exact agent trajectory that L4 scored in the MLflow UI and drill into individual tool calls.

The trace ID is stored on `AgentResult.mlflow_trace_id` and included in `task_results` for cross-referencing.

### Scoring

```
score = mean(all_dimension_scores) / 5
```

The overall score uses the actual 1-5 dimension scores, normalized to 0-1. This preserves granularity — a test scoring 5/5/5/5 (score=1.0) is meaningfully different from 3/3/3/3 (score=0.6).

---

## Level 5: Output Eval

**File**: `levels/output_eval.py` (550 lines)
**Requires agent + workspace + MCP tools. The core controlled experiment.**

L5 is the most comprehensive level. It evaluates across three dimensions — not just what the agent said, but what it actually built — using a controlled experiment design:

### 6-phase evaluation pipeline

```
Phase 1:  Run agent WITH skill
Phase 2:  Run agent WITHOUT skill (cached baseline)
Phase 3:  Response text grading ─── WITH vs WITHOUT semantic comparison
Phase 4a: Asset verification (trace) ─ Tool calls succeeded? IDs returned? Params correct?
Phase 4b: Live asset verification ──── Do resources actually exist in Databricks? (SDK)
Phase 5:  Source of truth comparison ── Do created assets match expectations?
```

### Phase 1: Run agent WITH skill

Same as L2/L4 — `run_agent_sync_wrapper(prompt, skill_md, mcp_config)`. The agent gets SKILL.md in its system prompt and runs against a real Databricks workspace.

### Phase 2: Run agent WITHOUT skill (cached baseline)

The same prompt is run **without** the skill: `run_agent_sync_wrapper(prompt, skill_md=None, mcp_config)`. The agent has access to the same MCP tools but doesn't get the SKILL.md instructions.

This baseline is cached by prompt hash (`sha256(prompt)[:12]`). Since the model and prompt don't change, the baseline is stable across runs.

### Phase 3: Response text grading (WITH vs WITHOUT)

Both responses are graded against the same expectations from `ground_truth.yaml` using the semantic grader (see [Semantic Grading Pipeline](#semantic-grading-pipeline)). Each assertion is classified:

| Label | WITH | WITHOUT | Meaning |
|-------|------|---------|---------|
| **POSITIVE** | pass | fail | Skill taught the agent something it didn't know |
| **REGRESSION** | fail | pass | Skill confused the agent — it was better without |
| **NEEDS_SKILL** | fail | fail | Neither response handles this — skill must add content |
| **NEUTRAL** | pass | pass | Agent already knows this — skill adds no value here |

This classification is the core signal: **POSITIVE** assertions prove the skill's value, **REGRESSION** assertions reveal harm, **NEEDS_SKILL** identifies gaps in the skill's coverage.

### Phase 4: Asset verification

Evaluating response text alone isn't enough. A skill like `databricks-genie` should be verified by checking that the Genie Space was **actually created**, has the **right tables**, and includes **sample questions** — not just that the agent's text mentions these things.

The asset verification phase inspects the agent's execution trace through 4 checks:

**Check 1 — Tool call success**: Every MCP tool call should have succeeded. If `create_or_update_genie` returned an error, the asset wasn't created regardless of what the response text says. The evaluator checks each MCP tool call's result for error markers (`"error":`, `traceback`, `exception`, `failed`).

**Check 2 — Resource ID returned**: For creation tools (`create_or_update_genie`, `create_or_update_dashboard`, `create_job`, `create_or_update_pipeline`, etc.), the evaluator parses the tool result as JSON and looks for ID fields: `space_id`, `dashboard_id`, `job_id`, `pipeline_id`, `id`, `resource_id`. If no ID is found, the resource likely wasn't created.

**Check 3 — Tool input parameters**: Verify the agent passed the correct parameters. Defined in `ground_truth.yaml` under `expectations.asset_verification.expected_tool_params`:

```yaml
asset_verification:
  expected_tool_params:
    mcp__databricks__create_or_update_genie:
      display_name: "Sales Analytics"
      table_identifiers: ["ac_demo.dc_assistant.customers"]
      sample_questions: "*"          # wildcard: just check param exists
```

Parameter matching supports three modes:
- **Exact match**: `display_name: "Sales Analytics"` — case-insensitive substring match against the actual value
- **List containment**: `table_identifiers: ["table_a", "table_b"]` — all listed items must appear in the actual list
- **Wildcard**: `sample_questions: "*"` — parameter must exist with any non-null value

The evaluator checks the **last** matching tool call (in case the agent retried).

**Check 4 — LLM asset assertions**: Freeform assertions evaluated by an LLM judge that reads the full tool call log (inputs + results):

```yaml
asset_verification:
  assertions:
    - "The create_or_update_genie tool returned a space_id in its result"
    - "The tool was called with at least one sample question about customer demographics"
```

### Phase 4b: Live asset verification (Databricks SDK)

Trace-based checks (Phase 4) verify what the agent *said* it did. Phase 4b verifies what *actually exists* in the Databricks workspace by making live SDK calls.

This is configured via `expectations.asset_verification.verify_live` in `ground_truth.yaml`:

```yaml
asset_verification:
  verify_live:
    - resource_type: genie_space
      extract_id_from: mcp__databricks__create_or_update_genie
      id_field: space_id
      checks:
        - field: display_name
          operator: contains
          value: "Sales Analytics"
        - field: table_identifiers
          operator: length_gte
          value: 1
```

The evaluator:
1. **Extracts the resource ID** from the agent's tool call results (e.g., `space_id` from the `create_or_update_genie` result)
2. **Calls the Databricks SDK** to fetch the live resource (e.g., `client.genie.get_space(space_id)`)
3. **Checks properties** against the spec using operators: `eq`, `contains`, `exists`, `length_gte`, `gte`, `lte`

Supported resource types:
- `genie_space` → `client.genie.get_space()`
- `dashboard` → `client.lakeview.get()`
- `job` → `client.jobs.get()`
- `pipeline` → `client.pipelines.get()`

If the Databricks SDK is not installed, live verification is gracefully skipped with `"skip"` feedback entries. Live verification feedbacks are included in the asset pass rate for scoring.

### Phase 5: Source of truth comparison

If `eval/source_of_truth/` files exist and the test case references them, the evaluator compares the agent's actual output against the expected content:

```yaml
expectations:
  source_of_truth:
    file: expected_genie_space.json
    mandatory_facts:
      - "ac_demo.dc_assistant.customers"
      - "sample_questions"
```

Two-step comparison:
1. **Mandatory facts**: Case-insensitive substring search of each fact against the agent's response text + all tool call results concatenated together.
2. **LLM structural comparison**: An LLM scores the match on three dimensions (each 1-10): structural match, content accuracy, completeness.

### Task scoring

Each test case's score is a weighted combination:

| Weight | Dimension | Condition |
|--------|-----------|-----------|
| 50% | Response text score | Always present |
| 30% | Asset verification pass rate | If `asset_verification` defined |
| 20% | Source of truth score | If `source_of_truth` defined |

If only response + assets (no SoT): 60% response, 40% assets.
If only response (no assets, no SoT): 100% response.

### Fallback mode

If the full semantic grader isn't available (import error), L5 falls back to simple assertion checking — substring matching for `expected_facts` and regex for `expected_patterns`. Less precise but always works.

---

## Semantic Grading Pipeline

**File**: `grading/semantic_grader.py` (747 lines)

The grading pipeline used by L5 Phase 3 implements a 3-phase hybrid approach to grade assertions against agent responses:

```
Phase 1: Deterministic (zero LLM cost)
─────────────────────────────────────
expected_patterns → regex match
expected_facts    → case-insensitive substring match
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
- **Phase 2** can verify behavioral assertions ("agent called the correct MCP tool") by reading the execution transcript — it sees both the response AND what the agent actually did
- **Phase 3** catches semantic equivalences (e.g., `"example_questions"` matches `"sample_questions"`)

Fact failures from Phase 1 get a second chance in Phase 2/3 — if the LLM confirms the content is present using different wording, the fact is upgraded to pass.

### The grade_with_without() flow

This is the core function that L5 Phase 3 calls:

1. **Grade WITH-skill response**: Run all three phases (deterministic → agent-based → semantic) against the WITH-skill response using the full assertion set from `ground_truth.yaml`.

2. **Grade WITHOUT-skill response**: Run deterministic checks (patterns + facts) first. For freeform assertions and guidelines, use agent-based grading with the WITHOUT transcript.

3. **Classify each assertion**: For each assertion at the same index in both result lists, compare WITH vs WITHOUT pass/fail to produce POSITIVE/REGRESSION/NEEDS_SKILL/NEUTRAL.

4. **Compute diagnostics**: Pass rates, effectiveness delta, regression rate — all fed into the scoring formula.

### Model fallback chain

**File**: `grading/llm_backend.py`

When an LLM call fails with rate limiting (`REQUEST_LIMIT_EXCEEDED`), the backend automatically cycles through fallback models:

1. Primary model: 3 retries with exponential backoff
2. Fallback chain: GPT-5-2 → Gemini-3-1-Pro → Claude Opus 4.5 → GPT-5 → Claude Sonnet 4.6

Each fallback model gets 3 retries. If all exhausted: returns score 0.0. Workspace-level errors (403, auth failures, network errors) skip the fallback chain entirely since they indicate the whole workspace is unreachable.

---

## Scoring Formulas

### L1 Unit Tests

```
score = passed_checks / total_checks       # 0-1
pass_threshold = 0.5
```

### L2 Integration Tests

```
score = successful_tests / total_tests     # 0-1 (binary per test case)
```

### L3 Static Eval

```
overall_score = mean(all evaluated dimension scores)  # 1-10 scale
coverage_factor = dimensions_evaluated / 10           # penalizes missing dims
normalized_score = (overall_score / 10.0) * coverage_factor  # 0-1
dimension_passes_at >= 6
```

### L4 Thinking Eval

```
score = mean(all_dimension_scores) / 5    # 0-1, preserves continuous granularity
dimension_passes_at >= 3 (out of 5)
```

Each dimension is scored 1-5 by the LLM judge. The overall score averages all dimension scores across all test cases and normalizes to 0-1. This preserves granularity — 5/5/5/5 (score=1.0) is meaningfully different from 3/3/3/3 (score=0.6).

### L5 Output Eval — per-task score

Each test case is scored across three dimensions (when all are present):

```
task_score = 0.50 * response_score + 0.30 * asset_pass_rate + 0.20 * sot_score
```

If no source of truth defined: `0.60 * response + 0.40 * assets`.
If no asset verification defined: `1.0 * response`.

### L5 Response score — semantic grader formula

```python
response_score = max(0.0, min(1.0,
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

### Suite composite

```
composite_score = mean(all level scores)          # 0-1
```

---

## Agent MCP Configuration (L2, L4, L5)

L2/L4/L5 run a real Claude Code agent that needs MCP tools (e.g., a Databricks MCP server). The flow:

```
dse evaluate ./my-skill --levels all --mcp-json .mcp.json
  → orchestrator runs L2/L4/L5
    → run_agent_sync_wrapper(mcp_config=databricks_servers)
      → Claude Agent SDK subprocess
        → starts Databricks MCP server independently
        → agent uses Databricks tools (execute_sql, create_genie, etc.)
```

The `--mcp-json` flag tells the evaluator where to find the Databricks MCP server config. It auto-discovers `.mcp.json` from parent directories if not specified.

---

## MLflow Integration

The orchestrator creates a single MLflow run with:
- **Tags**: `skill_name`, `eval_type`, `levels`, `framework_version`
- **Metrics**: Per-level scores (`L1/unit/score`, `L3/static/score`, etc.)
- **Artifacts**: `evaluation.json`, `report.html`

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

[GEPA](https://github.com/gepa-ai/gepa) (Generalized Evolutionary Prompt Architect) treats the SKILL.md as a text artifact to optimize. The modules are extracted and ready — the `dse optimize` CLI command is scaffolded.

### Presets

| Preset | Max Metric Calls | Typical Time |
|--------|-----------------|-------------|
| `minimal` | 3 | ~2 min |
| `quick` | 5 (x components) | ~10 min |
| `standard` | 5 (x components, minibatch) | ~30 min |
| `thorough` | 5 (x components, detailed) | ~90 min |

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
      trace_expectations:              # Checked against execution trace (L2, L4)
        required_tools: [mcp__databricks__tool]
        banned_tools: [Bash]
        tool_limits: {Bash: 3}
        token_budget: {max_total: 100000}
      asset_verification:              # L5: verify created resources (Phase 4)
        expected_tool_params:           # Check tool inputs
          mcp__databricks__create_or_update_genie:
            display_name: "Sales Analytics"
            table_identifiers: ["catalog.schema.table"]
            sample_questions: "*"       # wildcard: param must exist, any value
        assertions:                     # Freeform checks on tool results (LLM-evaluated)
          - "The tool returned a space_id confirming creation"
          - "Sample questions reference actual column names"
      source_of_truth:                 # L5: compare against expected output (Phase 5)
        file: expected_output.json      # File in eval/source_of_truth/
        mandatory_facts:                # Must appear in agent output + tool results
          - "catalog.schema.table"
          - "sample_questions"
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
    "criteria": {"self_contained": 8, "no_conflicts": 9, "...": "..."},
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
