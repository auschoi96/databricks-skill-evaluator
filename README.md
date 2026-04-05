# databricks-skill-evaluator

Evaluate and optimize [Claude Code skills](https://docs.anthropic.com/en/docs/agents-and-tools/agent-skills) against real Databricks workspaces. Tests whether a skill actually improves agent behavior using a 5-level testing pyramid, with results logged to Databricks MLflow.

## Directory Structure

This repo uses a standard layout. Everything has a designated place:

```
databricks-skill-evaluator/
  skills/                        # YOUR SKILLS GO HERE
    my-skill/                    # Each skill is its own folder
      SKILL.md                   # Required: the skill file
      *.md                       # Optional: reference files
      eval/                      # Created by `dse init` (test config)
  mcps/                          # MCP SERVER REPOS GO HERE
    databricks-mcp-server/       # Clone MCP server repos into this folder
    databricks-tools-core/       # Supporting libraries go here too
  .mcp.json                      # MCP config (points to mcps/)
  src/skill_evaluator/           # Framework source (don't modify)
  skill-example/                 # Reference template (read-only)
```

**Rules:**
- Put skills in `skills/`. One folder per skill. Each must contain a `SKILL.md`.
- Put MCP server repos in `mcps/`. The `.mcp.json` at the repo root references them.
- Don't create subfolders inside `skills/my-skill/` except `eval/` (created by `dse init`).
- Don't put skills or MCP repos anywhere else in the repo.

## Setup (Step by Step)

### Step 1: Clone and install

```bash
git clone <this-repo-url>
cd databricks-skill-evaluator
pip install -e .
```

Verify the CLI is installed:

```bash
dse --help
```

### Step 2: Set up MCP servers (required for agent-based evaluation)

If your skill uses MCP tools (e.g., Databricks tools like `create_or_update_genie`), you need the MCP server installed. Clone the repos into `mcps/`:

```bash
# Clone into the mcps/ directory (NOT the repo root)
git clone <databricks-mcp-server-url> mcps/databricks-mcp-server
git clone <databricks-tools-core-url> mcps/databricks-tools-core

# Install them
./setup.sh --with-mcp
```

This installs the MCP server packages so the evaluator can spawn agents that call MCP tools.

**If your skill does NOT use MCP tools**, skip this step entirely.

### Step 3: Authenticate with Databricks

```bash
dse auth --profile <your-databricks-profile> --catalog main --schema skill_test
```

This saves your workspace config to `~/.dse/config.yaml`. The `--profile` must match a profile in your `~/.databrickscfg` file.

If you don't have a Databricks profile yet:

```bash
databricks auth login --host https://your-workspace.cloud.databricks.com
```

### Step 4: Add your skill

Copy your skill folder into `skills/`:

```bash
cp -r /path/to/my-skill skills/my-skill
```

Your skill folder must contain a `SKILL.md` with YAML frontmatter:

```yaml
---
name: my-skill
description: "What this skill does and when to use it"
---

# My Skill

Instructions for the agent...
```

Verify it's detected:

```bash
dse list
```

You should see:

```
Available skills:
  my-skill                       [needs init]
```

### Step 5: Initialize evaluation config

```bash
dse init my-skill
```

This creates `skills/my-skill/eval/` with template files:

```
skills/my-skill/eval/
  ground_truth.yaml           # Test cases — prompts + expected outputs
  manifest.yaml               # Scorer config
  thinking_instructions.md    # What good reasoning looks like
  output_instructions.md      # What correct output looks like
```

### Step 6: Write your test cases

Open `skills/my-skill/eval/ground_truth.yaml` and replace the TODO placeholders with real test cases:

```yaml
test_cases:
  - id: basic_usage
    inputs:
      prompt: "Create a dashboard showing sales by region"
    expectations:
      expected_facts:
        - "sales"
        - "region"
      assertions:
        - "The agent creates a dashboard with regional sales data"
      expected_patterns:
        - pattern: "create_or_update_dashboard"
          min_count: 1
          description: "Must call the create dashboard tool"
      trace_expectations:
        required_tools:
          - mcp__databricks__create_or_update_dashboard
```

Also edit:
- `thinking_instructions.md` — describe what good reasoning looks like for your skill
- `output_instructions.md` — describe what correct output looks like

See [skill-example/](skill-example/) for a complete annotated template.

### Step 7: Run evaluation

**Quick eval** (seconds, no agent needed):

```bash
dse evaluate my-skill --levels unit,static
```

**Full eval** (minutes, runs real Claude agents against Databricks):

```bash
dse evaluate my-skill --levels all
```

You don't need `--mcp-json` — the evaluator automatically uses the `.mcp.json` at the repo root.

### Step 8: Review results

Every evaluation generates:
- `skills/my-skill/eval/report.html` — visual report with per-check details
- `skills/my-skill/eval/evaluation_results.json` — machine-readable results
- An MLflow run in your configured experiment

## Evaluation Levels

Levels run in cost order. L1 and L3 are fast gates; L2/L4/L5 are agent-based.

| Level | Name | What It Tests | Needs Agent? | Time |
|-------|------|---------------|-------------|------|
| L1 | **Unit Tests** | Code syntax, broken links, YAML validity | No | Seconds |
| L3 | **Static Eval** | SKILL.md quality (10 criteria via LLM judge) | No | ~30s |
| L2 | **Integration** | End-to-end MCP tool calls against Databricks | Yes | Minutes |
| L4 | **Thinking** | Agent reasoning: efficiency, clarity, recovery | Yes | Minutes |
| L5 | **Output** | WITH vs WITHOUT skill comparison | Yes | Minutes |

### L1: Unit Tests

Extracts every fenced code block from the skill's markdown files and validates syntax. Python blocks parsed with `ast.parse()`, SQL checked for structure, YAML validated with `yaml.safe_load()`, relative links verified. Zero LLM cost.

### L2: Integration Tests

Runs the real Claude Code agent against your Databricks workspace. Tests MCP tool connectivity, executes test cases from `ground_truth.yaml`, validates tool call success rates, and checks trace expectations.

### L3: Static Eval

An LLM judge evaluates the SKILL.md across 10 quality dimensions: self-contained, no conflicts, security, LLM-navigable structure, actionable instructions, scoped clearly, tool accuracy, examples valid, error handling, no hallucination triggers.

### L4: Thinking Eval

Evaluates HOW the agent reasons: efficiency (minimum tool calls?), clarity (confusion or backtracking?), recovery (error handling?), completeness (all steps finished?).

### L5: Output Eval (WITH vs WITHOUT)

The core controlled experiment. Each test case runs WITH the skill and WITHOUT it, then assertions are classified:

- **POSITIVE**: WITH passes, WITHOUT fails — skill taught something useful
- **REGRESSION**: WITH fails, WITHOUT passes — skill confused the agent
- **NEEDS_SKILL**: Both fail — skill doesn't cover this yet
- **NEUTRAL**: Both pass — agent already knows this

## CLI Reference

```bash
dse list                                    # Show available skills
dse auth --profile <name>                   # Authenticate with Databricks
dse init <skill>                            # Create eval config templates
dse evaluate <skill> --levels <levels>      # Run evaluation
dse optimize <skill> --preset <preset>      # Optimize SKILL.md (GEPA)
dse setup --profile <name>                  # Register MCP server in Claude Code
```

`<skill>` can be a name (looked up in `skills/`) or a path (e.g., `./my-skill`).

### Key flags for `dse evaluate`

| Flag | Purpose | Default |
|------|---------|---------|
| `--levels` | Comma-separated: unit, static, integration, thinking, output, all | `unit,static` |
| `--mcp-json` | Path to `.mcp.json` | Repo root `.mcp.json` |
| `--profile` | Databricks config profile | Saved config |
| `--experiment` | MLflow experiment path | `/Shared/skill-evals` |
| `--agent-model` | Claude model override | Default |
| `--agent-timeout` | Agent timeout in seconds | No timeout |
| `--suggest-improvements` | Generate improvement suggestions | Off |
| `--compare-baseline` | MLflow run ID to compare against | None |

## Iterative Improvement

1. **Run eval** — review the HTML report
2. **Read failure rationales** — each feedback explains WHY it failed
3. **Fix the SKILL.md** — use rationales to make targeted edits
4. **Rerun** — verify improvements
5. **Compare in MLflow** — track scores across runs

Write your `ground_truth.yaml` assertions BEFORE polishing the skill. The assertions define the specification. Then iterate on the SKILL.md until the judges pass.

## Scoring

| Level | Pass Threshold | Formula |
|-------|---------------|---------|
| L1: Unit | >= 50% | passed_checks / total_checks |
| L2: Integration | >= 50% | successful_tests / total_tests |
| L3: Static | >= 50% | (mean_dimension / 10) * coverage_factor |
| L4: Thinking | >= 50% | mean(dimension_scores) / 5 |
| L5: Output | >= 50% | 50% response + 30% assets + 20% source_of_truth |
| Composite | — | mean(all level scores) |

## Troubleshooting

| Error | Fix |
|-------|-----|
| `dse: command not found` | Run `pip install -e .` from the repo root |
| "No ~/.databrickscfg found" | Run `databricks auth login --host <URL>` |
| "Profile not found" | Check `~/.databrickscfg` for available profile names |
| "No SKILL.md found" | Make sure your skill folder has a `SKILL.md` at the top level |
| "No test cases in ground_truth.yaml" | Run `dse init <skill>`, then fill in the TODOs |
| "No such tool available: mcp__*" | MCP server not installed. Run `./setup.sh --with-mcp` |
| `dse list` shows nothing | Copy your skill folder into `skills/` |
| Agent timeout | Increase `--agent-timeout` or simplify test cases |

## Project Structure

```
src/skill_evaluator/
  cli.py                     # Click CLI (dse command)
  paths.py                   # Standard directory paths (skills/, mcps/)
  orchestrator.py            # Suite runner
  auth.py                    # Databricks auth + ~/.dse/config.yaml
  skill_discovery.py         # Parse SKILL.md frontmatter + references
  mcp_resolver.py            # Resolve .mcp.json for agent execution
  test_instructions.py       # Load eval/ config
  core/                      # Config, dataset, trace models
  levels/                    # L1-L5 evaluation implementations
  grading/                   # Semantic grader, LLM backend
  scorers/                   # Deterministic + LLM-based scorers
  optimize/                  # GEPA optimization
  reporting/                 # HTML report generator
```

## Full Documentation

- [skill-example/](skill-example/) — Ready-to-copy template with annotated files
- [example.md](example.md) — End-to-end walkthrough with the databricks-genie skill
- [TECHNICAL.md](TECHNICAL.md) — Deep dive into scoring formulas, grading pipeline, MLflow integration

## License

MIT
