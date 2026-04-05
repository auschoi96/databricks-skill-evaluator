---
name: skill-evaluator
description: "Evaluates Claude Code skills using a 5-level testing pyramid against real Databricks workspaces. Use when asked to evaluate a skill, test skill quality, check if a SKILL.md works, score a skill, audit skill content, or run a full eval. Supports quick eval (L1+L3 in seconds, no agent) and full agent-based eval (L2+L4+L5 in minutes). Logs results to MLflow for tracking and comparison."
---

# Skill Evaluator

You are an evaluation agent. Follow the workflow below step by step.

Levels run in cost order, not numerical order: L1 (free) and L3 (1 LLM call) run first as a quick gate. Agent-based levels L2, L4, L5 only run if the user requests full evaluation and the quick eval passes.

## Prerequisites

The `dse` CLI must be installed:

```bash
pip install -e /path/to/databricks-skill-evaluator
```

Verify with `dse --help`. For agent-based levels (L2/L4/L5), a Databricks workspace must be configured.

### MCP Server Setup (required for agent-based levels)

Agent-based levels (L2/L4/L5) spawn Claude agents that call MCP tools. If the skill under evaluation references MCP tools (e.g., `mcp__databricks__*`), those MCP servers must be **installed and importable** in the current Python environment.

The `.mcp.json` may reference a `.venv/bin/python` that doesn't exist locally. The evaluator will automatically fall back to the current Python interpreter, but the MCP server packages must be installed. Run:

```bash
./setup.sh --with-mcp
```

This installs `databricks-tools-core` and `databricks-mcp-server` (must be cloned into the repo root first). Without this, agent-based levels will fail with "No such tool available" errors.

**Not every skill requires MCP tools.** If the skill being evaluated doesn't reference MCP tools, skip this step and omit `--mcp-json` from `dse evaluate` commands.

---

## Dispatch

| User says | Start at |
|-----------|----------|
| "Check my skill" / "Is my SKILL.md good?" / "Score my skill" | Phase 0 (quick eval through Phase 2) |
| "Full evaluation" / "Test everything" / "Evaluate my skill" | Phase 0 (full eval through Phase 4) |
| "Help me set up evaluation" / "Init eval" | Phase 0 (stop after Phase 1 if no eval config) |
| "Why is my skill making things worse?" | Phase 0 then skip to L5 in Phase 3 |
| "Run integration tests" / "Run L2" | Phase 0 then skip to L2 in Phase 3 |
| "Optimize my skill" / "Improve my SKILL.md" | Phase 4 refinement loop |

Always ask the user what skill they want to evaluate if it is not immediately clear. 

If the request doesn't clearly match, run through Phase 2 (quick eval). Ask the user if they want to continue to agent-based levels.

---

## Phase 0: Authenticate

Check if `~/.dse/config.yaml` exists. If it does, skip to Phase 1.

If not, ask the user for their Databricks profile name (from `~/.databrickscfg`) and catalog/schema for test resources. Then run:

```bash
dse auth --profile DEFAULT --catalog main --schema default
```

Options: `--warehouse-id <id>`, `--experiment /Shared/skill-evals`.

**GATE**: If authentication fails:

| Error contains | Fix |
|---------------|-----|
| "No ~/.databrickscfg" | Run: `databricks auth login --host <WORKSPACE_URL>` |
| "Profile not found" | Check available profiles listed in the error |
| "Cannot connect" | Token may be expired — re-run `databricks auth login` |

---

## Phase 1: Discover the Skill and its MCP Tools

Ask the user for:
1. **Skill directory path** — the directory containing SKILL.md
2. **MCP tools location** — which `.mcp.json` has the MCP servers the skill needs (e.g., the Databricks MCP server). The skill being evaluated likely references MCP tools it doesn't define itself. Ask: "Where is the `.mcp.json` that defines the MCP tools this skill uses?" Common locations:
   - The parent project's `.mcp.json`
   - `~/.claude.json` (user's global Claude Code config)
   - A specific path the user provides

Run:

```bash
dse init <skill_dir>
```

This discovers the skill and scaffolds eval templates if missing.

**GATE — MCP config check**: If the user provided an `.mcp.json` path:
1. Verify the file exists.
2. Check that the MCP server packages are installed by running: `python -c "import databricks_mcp_server"` (or the relevant server package). If the import fails, tell the user to run `./setup.sh --with-mcp`.
3. Pass `--mcp-json <path>` to all subsequent `dse evaluate` commands. Without this, agent-based levels (L2/L4/L5) will have no MCP tools and fail.

If the skill does NOT reference MCP tools, skip this gate entirely — `--mcp-json` is not needed.

**GATE — eval config check**: If `eval/ground_truth.yaml` doesn't exist or contains only TODOs, STOP and tell the user:

> Fill in the TODO placeholders before running evaluation:
> - `ground_truth.yaml` — test cases (prompts + expected outputs)
> - `thinking_instructions.md` — what good reasoning looks like
> - `output_instructions.md` — what correct output looks like
>
> For the full ground_truth.yaml schema, read [TECHNICAL.md section Data Formats](TECHNICAL.md#data-formats).

If eval config is ready, proceed to Phase 2.

---

## Phase 2: Quick Eval (L1 + L3)

Run unit tests and static eval together — these are fast and don't require an agent:

```bash
dse evaluate <skill_dir> --levels unit,static
```

**Parse the output** for L1 and L3 scores. Present:

```
## Quick Eval Summary
- L1 Unit Tests: score% — N code blocks tested, N syntax errors
- L3 Static Eval: score/10 — dimensions < 6 are priority fixes
- Top issues: (2-3 most important findings)
```

**GATE**: If the user only asked for a quick check, STOP here. Otherwise ask: "Continue with agent-based testing? This runs real Claude agents against your Databricks workspace — several minutes per test case."

---

## Phase 3: Agent Eval (L2 + L4 + L5)

**IMPORTANT**: Always pass `--mcp-json <path>` from Phase 1. Without it, the spawned agent has no MCP tools.

Run all agent-based levels:

```bash
dse evaluate <skill_dir> --levels integration,thinking,output --mcp-json <path> --agent-timeout 300
```

Or run individual levels if the user asked for a specific one:

```bash
dse evaluate <skill_dir> --levels integration --mcp-json <path>
dse evaluate <skill_dir> --levels thinking --mcp-json <path>
dse evaluate <skill_dir> --levels output --mcp-json <path>
```

Optional flags: `--agent-model <model>`, `--agent-timeout <seconds>`, `--judge-model <model>`.

### L2: Integration Tests

Tests MCP tool connectivity and executes ground_truth test cases against a real Databricks workspace. Needs >= 80% tool success rate.

**GATE**: If MCP connectivity failures appear, STOP. The workspace or MCP config is broken — L4 and L5 will also fail.

### L4: Thinking Eval

Evaluates agent reasoning quality across 4 dimensions (scored 1-5 each): Efficiency, Clarity, Recovery, Completeness.

### L5: Output Eval (WITH vs WITHOUT)

The core controlled experiment. Each test case runs WITH the skill and WITHOUT, then assertions are classified:

- **POSITIVE**: WITH passes, WITHOUT fails. Skill taught something useful.
- **REGRESSION**: WITH fails, WITHOUT passes. Skill confused the agent. Fix priority #1.
- **NEEDS_SKILL**: Both fail. Skill doesn't cover this yet. Fix priority #2.
- **NEUTRAL**: Both pass. Agent already knows this — skill not needed here.

Score weighted: 50% response quality + 30% asset verification + 20% source-of-truth comparison.

---

## Phase 4: Report & Summary

After running levels, an HTML report is generated at `<skill_dir>/eval/report.html`. Present:

```
## Evaluation Complete

| Level | Score | Status |
|-------|-------|--------|
| L1: Unit Tests     | score%  | PASS/FAIL |
| L3: Static Eval    | score/10 | PASS/FAIL |
| L2: Integration    | score%  | PASS/FAIL |
| L4: Thinking       | score%  | PASS/FAIL |
| L5: Output (W/WO)  | score%  | PASS/FAIL |
| **Composite**      | **score%** | |

### Top Issues (prioritized)
1. REGRESSION: specific issue + which SKILL.md section to fix
2. NEEDS_SKILL: what content to add
3. LOW static score: dimension + recommendation

### Next Steps
- [ ] Fix regressions
- [ ] Add missing coverage for NEEDS_SKILL items
- [ ] Re-run evaluation to verify improvements
```

---

## Full Eval (all levels at once)

To run everything in one shot:

```bash
dse evaluate <skill_dir> --levels all --mcp-json <path> --suggest-improvements
```

Add `--experiment /Shared/skill-evals` to log to MLflow. Add `--compare-baseline <run_id>` to compare against a previous run.

---

## Iterative Refinement

1. **Run eval** — review the report
2. **Read failure rationales** — each feedback explains WHY it failed
3. **Fix the SKILL.md** — use rationales to make targeted edits
4. **Rerun the same levels** — verify improvements
5. **Compare in MLflow** — track scores across runs

Write your `ground_truth.yaml` assertions BEFORE polishing the skill. The assertions define the specification. Then iterate on the SKILL.md until the judges pass.

---

## Error Recovery

| Error | Fix |
|-------|-----|
| "No ~/.databrickscfg found" | Run: `databricks auth login --host <URL>` |
| "Profile 'X' not found" | Check available profiles in error message |
| "Cannot connect to workspace" | Re-run: `databricks auth login` |
| "No SKILL.md found in ..." | Verify the directory contains SKILL.md |
| "No test cases in ground_truth.yaml" | Run `dse init`, fill in TODOs |
| "MCP tool not reachable" | Check `.mcp.json` path, verify MCP server starts |
| "No such tool available: mcp__*" | MCP server failed to start. Check: (1) `./setup.sh --with-mcp` was run, (2) the MCP server package is importable in the current Python, (3) `.mcp.json` command path exists (the evaluator falls back to `sys.executable` if `.venv` is missing) |
| "REQUEST_LIMIT_EXCEEDED" | Wait 30s and retry |
| "Agent timeout" | Increase `--agent-timeout` or simplify the test case |

---

## Scoring

| Level | Pass | Formula |
|-------|------|---------|
| L1: Unit | >= 0.5 | passed_checks / total_checks |
| L2: Integration | >= 0.5 | successful_tests / total_tests |
| L3: Static | >= 0.5 | (mean_dimension / 10) * coverage_factor |
| L4: Thinking | >= 0.5 | mean(dimension_scores) / 5 |
| L5: Output | >= 0.5 | 50% response + 30% assets + 20% source_of_truth |
| Composite | — | mean(all level scores) |

For detailed scoring formulas, see [TECHNICAL.md section Scoring Formulas](TECHNICAL.md#scoring-formulas).