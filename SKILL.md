---
name: skill-evaluator
description: "Evaluate Claude Code skills using a 5-level evaluation pyramid. Use when asked to evaluate a skill, test skill quality, check if a SKILL.md works, score a skill, audit skill content, or run a full eval. Calls skill-evaluator MCP tools to authenticate, discover, test, and score skills against real Databricks workspaces."
---

# Skill Evaluator

You are an evaluation agent. When activated, you follow the workflow below **step by step**. Do not skip steps. Do not run the next step until you have presented results from the current step and confirmed whether to continue.

## Dispatch

Match the user's request to an entry point:

| User says | Start at |
|-----------|----------|
| "Check my skill" / "Is my SKILL.md good?" / "Score my skill" | **Step 1** (runs through Step 4 quick eval) |
| "Full evaluation" / "Test everything" / "Evaluate my skill" | **Step 1** (runs through Step 7) |
| "Help me set up evaluation" / "Init eval" | **Step 1** (stops after Step 2 if no eval config) |
| "Why is my skill making things worse?" | **Step 1** → skip to **Step 6** (L5 output eval) |
| "Run integration tests" / "Run L2" | **Step 1** → skip to **Step 5** |

If the request doesn't clearly match, start at **Step 1** and run through **Step 4** (quick eval). Ask the user if they want to continue to agent-based levels.

---

## Step 1: Authenticate

**Goal**: Ensure a workspace connection exists.

Check if `~/.dse/config.yaml` exists. If not, ask the user for:
- Databricks profile name (from `~/.databrickscfg`)
- Catalog and schema for test data
- (Optional) MLflow experiment path

Then call:
```
authenticate_workspace(profile, catalog, schema, experiment_path?)
```

**GATE**: If authentication fails, STOP. Show the error and ask the user to fix their Databricks config.

If `~/.dse/config.yaml` already exists, skip to Step 2.

---

## Step 2: Discover the Skill

**Goal**: Parse the skill directory and check readiness.

Ask the user for the skill directory path if not already known. Then call:
```
discover_skill(skill_dir)
```

**Present to user**:
- Skill name, description, file count
- Whether eval config exists (`has_eval_config`)

**GATE — eval config check**:
- If `has_eval_config` is **false**: call `init_eval_config(skill_dir)` to scaffold templates. Then STOP and tell the user:
  > "I created eval templates in `<skill_dir>/eval/`. You need to fill in the TODO placeholders in these files before I can run evaluation:
  > - `ground_truth.yaml` — your test cases (prompts + expected outputs)
  > - `thinking_instructions.md` — what good reasoning looks like for this skill
  > - `output_instructions.md` — what correct output looks like for this skill
  >
  > Come back when these are filled in and say 'evaluate my skill'."
- If `has_eval_config` is **true**: proceed to Step 3.

---

## Step 3: L1 — Unit Tests

**Goal**: Validate code block syntax and links. Fast, free, no agent.

Call:
```
run_unit_tests(skill_dir)
```

**Interpret**: Scan feedbacks for any with `value: "fail"`. These are broken code blocks (Python syntax errors, invalid YAML, malformed SQL) or dead markdown links.

**Present to user**:
```
## L1: Unit Tests — <PASS/FAIL> (<score as %>)
- Code blocks tested: <N>
- Syntax errors: <N>  (list each with file + block number)
- Dead links: <N>  (list each)
```

**GATE**: If there are syntax errors or dead links, flag them as **must-fix before continuing**. Ask: "Fix these issues first, or continue evaluation anyway?"
- If user says fix → STOP, show what to fix
- If user says continue → proceed to Step 4

Save the result for report generation later.

---

## Step 4: L3 — Static Eval

**Goal**: LLM judge scores the SKILL.md quality across 10 dimensions. 1 LLM call, no agent.

Call:
```
run_static_eval(skill_dir)
```

**Interpret**: Check `metadata.criteria` for per-dimension scores (1-10). Any dimension below 6 is a problem. Check `metadata.recommendations` for specific fixes.

**Present to user** as a scorecard:
```
## L3: Static Eval — <overall_score>/10

| Dimension               | Score | Status |
|-------------------------|-------|--------|
| Self-Contained          | 8     | PASS   |
| No Conflicting Info     | 9     | PASS   |
| Security                | 7     | PASS   |
| LLM-Navigable Structure| 8     | PASS   |
| Actionable Instructions | 5     | ⚠ LOW  |
| Scoped Clearly          | 8     | PASS   |
| Tools/CLI Accuracy      | 6     | PASS   |
| Examples Are Valid       | 10    | PASS   |
| Error Handling Guidance  | 4     | ⚠ LOW  |
| No Hallucination Triggers| 7    | PASS   |

### Recommendations
- <list each recommendation from metadata.recommendations>
```

Mark any dimension < 6 with "⚠ LOW" and bold it. These are the priority fixes.

**GATE — quick eval complete**:

If the user only asked for a quick check ("check my skill", "is this good?"), present a summary combining L1 + L3 results and STOP:
```
## Quick Eval Summary
- L1 Unit Tests: <score>%
- L3 Static Eval: <score>/10
- Top issues: <list the 2-3 most important findings>
- To run full agent-based evaluation, say "run full evaluation"
```

If the user asked for full evaluation, proceed to Step 5. Otherwise ask: "Want me to continue with agent-based testing? This runs real Claude Code agents against your Databricks workspace and takes several minutes per test case."

Save the result for report generation later.

---

## Step 5: L2 — Integration Tests

**Goal**: Run a real agent with the skill against the Databricks workspace. Tests tool connectivity and execution.

**Before running**, tell the user:
> "Starting integration tests. This runs a Claude Code agent for each test case in your ground_truth.yaml. Estimated time: ~2-5 minutes per test case."

Call:
```
run_integration_tests(skill_dir)
```

**Interpret**: Check feedbacks for tool connectivity failures, execution errors, and trace expectation violations (required tools not called, banned tools used).

**Present to user**:
```
## L2: Integration Tests — <PASS/FAIL> (<score as %>)

| Test Case | Status | Tools Called | Issues |
|-----------|--------|-------------|--------|
| <id>      | PASS   | create_or_update_genie | — |
| <id>      | FAIL   | (none)      | MCP tool not reachable |
```

**GATE**: If integration tests show MCP connectivity failures, STOP. The workspace or MCP server config is broken — agent-based levels will all fail. Tell the user what to fix.

Save the result for report generation later. Proceed to Step 6.

---

## Step 6: L4 + L5 — Thinking Eval & Output Eval

**Goal**: Evaluate agent reasoning quality (L4) and run the WITH vs WITHOUT skill comparison (L5). These are the deep evaluation levels.

### L4: Thinking Eval

Call:
```
run_thinking_eval(skill_dir, mcp_json_path?)
```

The `mcp_json_path` should point to the `.mcp.json` that configures the Databricks MCP server. If omitted, auto-discovers from parent directories.

**Present to user**:
```
## L4: Thinking Eval — <score as %>

| Test Case | Efficiency | Clarity | Recovery | Completeness |
|-----------|-----------|---------|----------|--------------|
| <id>      | 4/5       | 5/5     | 3/5      | 5/5          |

Key findings:
- <list notable reasoning issues from feedbacks>
```

### L5: Output Eval (WITH vs WITHOUT)

Call:
```
run_output_eval(skill_dir, mcp_json_path?)
```

This is the core controlled experiment: each test case runs twice (WITH skill, WITHOUT skill) and assertions are classified.

**Present to user** with classification breakdown:
```
## L5: Output Eval — <score as %>

| Test Case | Assertion | WITH | WITHOUT | Classification |
|-----------|-----------|------|---------|---------------|
| <id>      | "calls create_or_update_genie" | PASS | FAIL | ✅ POSITIVE |
| <id>      | "includes sample questions"     | FAIL | PASS | 🔴 REGRESSION |
| <id>      | "handles bad table error"       | FAIL | FAIL | 🟡 NEEDS_SKILL |

### Classification Summary
- ✅ POSITIVE: <N> — Skill is helping
- 🔴 REGRESSION: <N> — Skill is hurting (FIX THESE FIRST)
- 🟡 NEEDS_SKILL: <N> — Skill must add this content
- ⚪ NEUTRAL: <N> — Agent already knows this
```

**Classification reference** (for interpreting results):
- **POSITIVE**: WITH passes, WITHOUT fails → skill taught something useful
- **REGRESSION**: WITH fails, WITHOUT passes → skill confused the agent. Fix priority #1.
- **NEEDS_SKILL**: Both fail → skill doesn't cover this yet. Fix priority #2.
- **NEUTRAL**: Both pass → agent already knows this, skill not needed here.

Save both results for report generation. Proceed to Step 7.

---

## Step 7: Generate Report & Final Summary

**Goal**: Produce the HTML report and present a final summary.

Call `generate_report` with all collected results:
```
generate_report(skill_dir, results={
  "unit": <L1 result>,
  "static": <L3 result>,
  "integration": <L2 result if run>,
  "thinking": <L4 result if run>,
  "output": <L5 result if run>
})
```

Only include levels that were actually run.

**Present final summary to user**:
```
## Evaluation Complete

| Level | Score | Status |
|-------|-------|--------|
| L1: Unit Tests     | <score>%  | PASS/FAIL |
| L3: Static Eval    | <score>/10 | PASS/FAIL |
| L2: Integration    | <score>%  | PASS/FAIL |
| L4: Thinking       | <score>%  | PASS/FAIL |
| L5: Output (W/WO)  | <score>%  | PASS/FAIL |
| **Composite**      | **<score>%** | |

### Top Issues (prioritized)
1. 🔴 REGRESSION: <specific issue + which SKILL.md section to fix>
2. 🟡 NEEDS_SKILL: <what content to add to SKILL.md>
3. ⚠ LOW static score: <dimension + recommendation>

### Concrete Suggestions
- <Specific edits to SKILL.md sections, referencing line numbers or headings>

### Next Steps
- [ ] Fix regressions in SKILL.md
- [ ] Add missing coverage for NEEDS_SKILL items
- [ ] Re-run evaluation to verify improvements

📄 HTML report saved to: `<skill_dir>/eval/report.html`
```

---

## Tool Reference

| Tool | Level | Agent? | Purpose |
|------|-------|--------|---------|
| `authenticate_workspace` | Setup | No | Connect to Databricks workspace |
| `discover_skill` | Setup | No | Parse skill directory, check eval readiness |
| `init_eval_config` | Setup | No | Scaffold eval/ templates for user to fill in |
| `run_unit_tests` | L1 | No | Validate code block syntax and markdown links |
| `run_static_eval` | L3 | No | LLM judge scores SKILL.md quality (10 dimensions, 1-10) |
| `run_integration_tests` | L2 | Yes | Real agent execution against Databricks |
| `run_thinking_eval` | L4 | Yes | Agent reasoning: efficiency, clarity, recovery |
| `run_output_eval` | L5 | Yes | WITH vs WITHOUT skill controlled experiment |
| `generate_report` | Report | No | Create HTML report from collected results |
