# Example: Evaluating the `databricks-genie` Skill

This walkthrough shows the complete process of taking a Claude Code skill, setting up evaluation, **writing your own test cases and criteria**, running all 5 levels, reviewing results, and optimizing the skill based on feedback.

We'll use the `databricks-genie` skill from [ai-dev-kit](https://github.com/databricks-solutions/ai-dev-kit) as our example.

> **You write the evals.** The framework provides structure, scoring, and tooling — but the test cases, assertions, thinking criteria, and output expectations are yours to create. You know your skill's domain better than any automated system. The quality of your evaluation is directly proportional to the quality of the test cases you write.

---

## Prerequisites

- A Databricks workspace with Unity Catalog enabled
- A `~/.databrickscfg` profile configured (via `databricks auth login`)
- An MCP server that provides the tools your skill references (e.g., the `databricks-mcp-server`)
- The skill directory you want to evaluate

```bash
pip install databricks-skill-evaluator
```

---

## Step 1: Authenticate with Databricks

Before any evaluation, connect to your workspace. The framework needs this to run agent-based tests and log results to MLflow.

```bash
dse auth \
  --profile e2-demo-field-eng \
  --catalog ac_demo \
  --schema dc_assistant \
  --experiment "/Users/you@databricks.com/GenAI/skill-evals"
```

Output:
```
Authenticated as profile 'e2-demo-field-eng' on https://e2-demo-field-eng.cloud.databricks.com
  Catalog: ac_demo
  Schema: dc_assistant
  Warehouse: 01370556fad60fda
  Experiment: /Users/you@databricks.com/GenAI/skill-evals
Config saved to ~/.dse/config.yaml
```

This saves your config to `~/.dse/config.yaml` so you don't need to pass these flags every time.

---

## Step 2: Prepare Your Skill

A skill is a directory containing a `SKILL.md` file with YAML frontmatter. Here's what `databricks-genie` looks like:

```
databricks-genie/
  SKILL.md              # Main skill file (required)
  spaces.md             # Reference: creating/managing Genie Spaces
  conversation.md       # Reference: Conversation API usage
```

The `SKILL.md` starts with frontmatter:
```yaml
---
name: databricks-genie
description: "Create and query Databricks Genie Spaces for natural language
  SQL exploration. Use when building Genie Spaces, exporting and importing
  Genie Spaces, or asking questions via the Genie Conversation API."
---
```

The rest of the file contains instructions, MCP tool references, code examples, and documentation that teaches Claude how to work with Genie Spaces.

---

## Step 3: Initialize Evaluation Config

Generate the `eval/` directory with template files:

```bash
dse init ./databricks-genie
```

Output:
```
Initialized eval config for 'databricks-genie' in databricks-genie/eval
Files created:
  ground_truth.yaml
  manifest.yaml
  thinking_instructions.md
  output_instructions.md
```

This creates:
```
databricks-genie/
  SKILL.md
  spaces.md
  conversation.md
  eval/                           # NEW
    ground_truth.yaml             # Test cases (you fill these in)
    manifest.yaml                 # Scorer configuration
    thinking_instructions.md      # Custom criteria for reasoning eval (L4)
    output_instructions.md        # Custom criteria for output eval (L5)
    source_of_truth/              # Expected outputs for comparison (L5)
```

---

## Step 4: Write Your Test Cases

This is the most important step. **You** write the test cases — the framework does not generate them for you. Think about the key workflows your skill teaches, the edge cases that matter, and the mistakes an agent might make without your skill's guidance.

Edit `eval/ground_truth.yaml` with test cases that exercise your skill's key workflows. Each test case has a prompt, expected outputs, and evaluation criteria.

Here's how we wrote test cases for databricks-genie:

```yaml
metadata:
  skill_name: databricks-genie
  version: 0.1.0

test_cases:
  # Test 1: Simple Genie Space creation
  - id: create_simple_space
    inputs:
      prompt: >
        Create a Genie Space called 'Sales Analytics' using the table
        ac_demo.dc_assistant.customers with sample questions about
        customer demographics
    expectations:
      expected_facts:
        - "ac_demo.dc_assistant.customers"
        - "sample_questions"
        - "Sales Analytics"
      expected_patterns:
        - pattern: "create_or_update_genie"
          min_count: 1
          description: "Must call the create_or_update_genie MCP tool"
      assertions:
        - "The response attempts to actually invoke the create_or_update_genie MCP tool"
        - "The response includes sample questions related to customer demographics"
      guidelines:
        - "The agent should use MCP tools, not write Python code to call REST APIs"
      trace_expectations:
        required_tools:
          - mcp__databricks__create_or_update_genie
        banned_tools: []
        tool_limits:
          Bash: 3
    metadata:
      category: happy_path
      difficulty: easy

  # Test 2: Ask a question to a Genie Space
  - id: ask_question
    inputs:
      prompt: >
        Ask the Genie Space with ID '01f2abc123' this question:
        'What are the top 5 customers by revenue?'
    expectations:
      expected_facts:
        - "01f2abc123"
        - "top 5 customers"
      expected_patterns:
        - pattern: "ask_genie"
          min_count: 1
          description: "Must call the ask_genie MCP tool"
      assertions:
        - "The response invokes ask_genie with the correct space_id and question"
      trace_expectations:
        required_tools:
          - mcp__databricks__ask_genie
    metadata:
      category: happy_path
      difficulty: easy

  # Test 3: Error recovery
  - id: error_recovery_bad_table
    inputs:
      prompt: "Create a Genie Space using the table ac_demo.nonexistent.fake_table"
    expectations:
      assertions:
        - "The agent handles the error when the table doesn't exist"
        - "The agent does NOT silently succeed with a broken configuration"
    metadata:
      category: error_recovery
      difficulty: hard
```

### Test case anatomy

| Field | Purpose |
|-------|---------|
| `inputs.prompt` | The user request sent to the agent |
| `expected_facts` | Substring matches checked deterministically (zero LLM cost) |
| `expected_patterns` | Regex patterns checked deterministically |
| `assertions` | Freeform checks evaluated by an LLM judge |
| `guidelines` | Quality guidelines evaluated semantically |
| `trace_expectations` | Tool usage requirements checked against the execution trace |
| `metadata.category` | Used for stratified train/val splitting (`happy_path`, `error_recovery`, `edge_case`) |

### How to think about writing test cases

1. **Start with your skill's core value.** What does this skill teach that the LLM doesn't already know? Write prompts that test exactly that.
2. **Cover the happy path first.** The most common workflows your users will run.
3. **Add edge cases.** What happens when inputs are wrong, resources don't exist, or parameters are missing?
4. **Be specific in expectations.** Vague assertions like "response is correct" give weak signal. Specific assertions like "response calls create_or_update_genie with both tables" give strong signal.
5. **Use trace expectations for tool behavior.** If your skill teaches the agent to use specific MCP tools, assert that in `trace_expectations.required_tools`.

Aim for **8+ test cases** to enable train/val splitting during optimization.

---

## Step 5: Write Your Custom Evaluation Instructions

These files define what "good" looks like for **your** skill specifically. The framework provides the scoring engine, but you define the rubric.

### `eval/thinking_instructions.md` (Level 4 criteria)

You write these to tell the LLM judge what "good reasoning" looks like for your specific skill. Think about: How many tool calls should a task take? Which tools are preferred? What should happen on errors?

```markdown
# Thinking Evaluation Criteria for databricks-genie

## Efficiency
- Creating a simple Genie Space should take 1-3 tool calls
- Agent should NOT write Python code when MCP tools are available
- Agent should NOT use Bash to invoke the databricks CLI

## Recovery
- If create_or_update_genie fails with invalid table, check the table exists first
- If warehouse not found, list warehouses and pick a running one

## Completeness
- Must actually call create_or_update_genie (not just describe how)
- Must include sample_questions in the creation call
```

### `eval/output_instructions.md` (Level 5 criteria)

You write these to define what correct output looks like for your skill's tasks. What artifacts should the agent produce? What facts must be present?

```markdown
# Output Evaluation Criteria for databricks-genie

## Expected Artifacts
- A Genie Space should be created (verifiable via get_genie)
- The space should have the correct tables attached
- Sample questions should be included

## Mandatory Facts
Defined per test case in ground_truth.yaml under expectations.expected_facts.
```

---

## Step 6: Run the Evaluation

### Quick check (no agent needed)

Start with L1 (unit tests) and L3 (static eval) to validate your skill without running the agent:

```bash
dse evaluate ./databricks-genie \
  --levels unit,static \
  --mcp-json /path/to/your/.mcp.json
```

Output:
```
Skill: databricks-genie
  Description: Create and query Databricks Genie Spaces for natural language SQL...
  Reference files: 3
  MCP tool references: 27

Running levels: unit, static
Experiment: /Users/you@databricks.com/GenAI/skill-evals

============================================================
Running Level 1: UNIT
============================================================
  Score: 1.00 | Feedbacks: 46 | Duration: 0.0s

============================================================
Running Level 3: STATIC
============================================================
  Score: 0.80 | Feedbacks: 18 | Duration: 4.1s

============================================================
RESULTS: databricks-genie
============================================================
  L1: unit            100% [PASS]
  L3: static          80%  [PASS]
  ────────────────────────────────────────
  Composite:        90%

MLflow run: a36ec88de40342d4ad2f2abcffb1248d
HTML report: databricks-genie/eval/report.html
```

**What each level checks:**

- **L1 (Unit)**: Validates all code blocks in SKILL.md and reference files are syntactically correct (Python, SQL, YAML). Checks for broken markdown links. Zero cost.
- **L3 (Static)**: An LLM judge evaluates the SKILL.md itself across 10 dimensions: is it self-contained? Are tool references accurate? Are examples valid? Is the structure navigable? Costs ~1 LLM call.

### Full evaluation (with agent)

Run all 5 levels including agent-based testing:

```bash
dse evaluate ./databricks-genie \
  --levels all \
  --mcp-json /path/to/your/.mcp.json \
  --suggest-improvements
```

This runs:
1. **L1 Unit Tests** - Code syntax validation (instant)
2. **L3 Static Eval** - SKILL.md quality assessment (seconds)
3. **L2 Integration Tests** - Real agent execution against Databricks (minutes)
4. **L4 Thinking Eval** - Agent reasoning quality from traces (minutes)
5. **L5 Output Eval** - WITH vs WITHOUT skill comparison (minutes)

For L4 and L5, the agent runs twice per test case: once WITH the skill and once WITHOUT. This controlled experiment isolates the skill's actual impact.

---

## Step 7: Review the HTML Report

Open `databricks-genie/eval/report.html` in a browser. The report shows:

- **Summary dashboard** with composite score, level count, duration, total checks
- **Per-level cards** with pass/fail status for every check
- **Feedback form** where you can record your assessment and export `feedback.json`

After reviewing, click **Save Feedback** to download `feedback.json`. This captures your human judgment about where the skill needs improvement.

---

## Step 8: Check MLflow

The evaluation run is logged to your Databricks MLflow experiment. Open the experiment in the Databricks workspace UI to see:

```
Experiment: /Users/you@databricks.com/GenAI/skill-evals
  Run: databricks-genie_eval_20260401_233632
    Tags:
      skill_name: databricks-genie
      eval_type: suite
      levels: unit,static,thinking,output
      framework_version: 0.1.0
    Metrics:
      suite/composite_score: 0.90
      L1/unit/score: 1.0
      L1/unit/code_blocks_tested: 30
      L2/static/score: 0.8
      L2/static/tool_accuracy: 3.0
      L2/static/self_contained: 4.0
    Artifacts:
      evaluation/dse_evaluation.json
      reports/report.html
```

You can compare runs across branches, track score trends over time, and drill into per-task metrics.

---

## Step 9: Optimize the Skill

Use the feedback from your review to optimize the SKILL.md with GEPA (Generalized Evolutionary Prompt Architect):

```bash
dse optimize ./databricks-genie \
  --feedback ./databricks-genie/eval/feedback.json \
  --preset quick \
  --apply
```

### How optimization works

1. **Reads your feedback** - Human annotations from the HTML report review
2. **Runs GEPA** - Evolutionary optimization that mutates the SKILL.md content
3. **Evaluates each mutation** - WITH/WITHOUT comparison using the semantic grader
4. **Selects the best** - Pareto frontier of candidates balancing effectiveness, token efficiency, and regression avoidance
5. **Applies the result** - Writes the optimized SKILL.md back to disk

### Optimization presets

| Preset | Iterations | Time | Use case |
|--------|-----------|------|----------|
| `minimal` | ~3 | ~2 min | Quick sanity check |
| `quick` | ~15 | ~10 min | Fast iteration during development |
| `standard` | ~50 | ~30 min | Thorough optimization |
| `thorough` | ~150 | ~90 min | Maximum quality (pre-release) |

---

## Step 10: Re-evaluate After Optimization

Run the evaluation again to measure improvement:

```bash
dse evaluate ./databricks-genie \
  --levels all \
  --mcp-json /path/to/your/.mcp.json \
  --compare-baseline a36ec88de40342d4ad2f2abcffb1248d
```

The `--compare-baseline` flag compares the new scores against the previous MLflow run, showing which metrics improved, regressed, or stayed the same.

---

## Iterating

The full cycle looks like:

```
Write/Edit SKILL.md
      |
      v
dse evaluate --levels unit,static    (quick feedback, seconds)
      |
      v
Fix obvious issues (broken examples, missing tool references)
      |
      v
dse evaluate --levels all            (full eval, minutes)
      |
      v
Review report.html, export feedback.json
      |
      v
dse optimize --feedback feedback.json --preset quick
      |
      v
dse evaluate --levels all --compare-baseline <run_id>
      |
      v
Repeat until composite score meets your quality bar
```

Start with L1+L3 for rapid iteration (seconds per cycle), then graduate to the full suite when the basic quality is solid.

---

## Bringing Your Own Skill

To evaluate any Claude Code skill:

1. **Prepare the skill directory** with a `SKILL.md` containing YAML frontmatter (`name` and `description`)
2. **Run `dse init`** to scaffold the eval config — this gives you empty templates
3. **Write your test cases** in `ground_truth.yaml` — prompts, expected facts, assertions, trace expectations
4. **Write your thinking criteria** in `thinking_instructions.md` — what does good reasoning look like for your skill?
5. **Write your output criteria** in `output_instructions.md` — what should the agent produce?
6. **Configure MCP** - point `--mcp-json` at a `.mcp.json` that has the servers your skill needs
7. **Run `dse evaluate`** and iterate

The framework is skill-agnostic. It works with any skill that follows the standard `SKILL.md` format, whether it's for Databricks, AWS, GCP, or any other domain.

### The framework does NOT write your evals for you

`dse init` creates template files with TODO placeholders. You fill them in based on your knowledge of:
- What workflows your skill teaches
- What tools the agent should use (and avoid)
- What a correct response looks like
- What errors the agent should handle gracefully
- How efficient the agent should be (tool call counts, token budgets)

The better your test cases, the better the evaluation signal, and the better the optimization results.
