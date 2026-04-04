# Skill Example

This is a reference template showing how to structure a skill directory for evaluation with `dse`. Copy this folder, rename it, and replace the placeholder content with your own.

## Minimum Required Structure

```
my-skill/
  SKILL.md                          # Required — the skill file itself
  eval/
    ground_truth.yaml               # Required — test cases (prompts + expectations)
    manifest.yaml                   # Required — scorer configuration
    thinking_instructions.md        # Required — reasoning quality rubric (L4)
    output_instructions.md          # Required — output quality rubric (L5)
```

- **`SKILL.md`** must have YAML frontmatter with `name` and `description` fields.
- **`eval/ground_truth.yaml`** must have at least one test case with an `inputs.prompt`. More test cases = better signal. Aim for 8+ to enable train/val splitting during optimization.
- **`eval/manifest.yaml`** configures which scorers run and their thresholds.
- **`eval/thinking_instructions.md`** and **`eval/output_instructions.md`** define the rubric the LLM judge uses for L4 and L5. Without these, the judge has no domain-specific criteria.

## The Skill Folder Should Be Self-Contained

The skill directory should contain **everything the skill needs to work** — not just the SKILL.md. When Claude Code loads a skill, it reads the SKILL.md and any files referenced from it. If your skill references additional documentation, examples, or configuration, those files must live inside the skill folder.

```
my-skill/
  SKILL.md                          # Main skill file
  reference-guide.md                # Additional docs referenced by SKILL.md
  api-patterns.md                   # More reference material
  examples/
    create-resource.py              # Standalone example scripts (if needed)
  eval/
    ground_truth.yaml
    manifest.yaml
    thinking_instructions.md
    output_instructions.md
```

### Why this matters

- **Portability**: Anyone can copy the skill folder into their project and it works.
- **Evaluation accuracy**: L1 unit tests validate code blocks in ALL `.md` files in the skill directory, not just SKILL.md. L3 static eval checks that reference links resolve. If referenced files are missing, your eval scores will drop.
- **Optimization**: When `dse optimize` rewrites the SKILL.md, it needs to understand the full context. Reference files that live outside the skill folder won't be visible.

### What goes in the skill folder vs. what doesn't

| In the skill folder | NOT in the skill folder |
|---------------------|------------------------|
| SKILL.md | The MCP server code that provides the tools |
| Reference docs (.md) the skill links to | `.mcp.json` config (passed via `--mcp-json` flag) |
| Eval config (`eval/`) | Databricks auth config (`~/.databrickscfg`) |
| Example code snippets embedded in docs | The `dse` CLI itself |

The skill teaches Claude **how** to use tools. The MCP server **provides** those tools. They live in separate places.

## Getting Started

1. **Copy this folder** and rename it:
   ```bash
   cp -r skill-example my-skill
   ```

2. **Write your SKILL.md** — replace the placeholder with your actual skill content. Include YAML frontmatter:
   ```yaml
   ---
   name: my-skill
   description: "What it does and when to use it"
   ---
   ```

3. **Add reference files** — if your skill needs additional documentation, add `.md` files to the folder and link them from SKILL.md using relative paths:
   ```markdown
   See [api-guide.md](api-guide.md) for detailed API documentation.
   ```

4. **Write your test cases** in `eval/ground_truth.yaml`. Start with:
   - 3-5 **happy path** cases covering the core workflows your skill teaches
   - 1-2 **error recovery** cases testing how the agent handles failures
   - 1-2 **edge cases** testing unusual inputs or multi-step workflows

5. **Write your evaluation rubrics** in `thinking_instructions.md` and `output_instructions.md`. These are specific to your skill — what does "good reasoning" and "correct output" look like for your domain?

6. **Run a quick eval** to validate the structure:
   ```bash
   dse evaluate ./my-skill --levels unit,static
   ```

7. **Run the full eval** when you're ready for agent-based testing:
   ```bash
   dse evaluate ./my-skill --levels all --mcp-json /path/to/.mcp.json
   ```

## Test Case Anatomy

Each test case in `ground_truth.yaml` can use these expectation types:

| Field | Type | Cost | Purpose |
|-------|------|------|---------|
| `expected_facts` | Substring match | Free | Values that must appear verbatim in the response |
| `expected_patterns` | Regex match | Free | Patterns (like tool names) that must appear |
| `assertions` | LLM judge | ~1 LLM call | Semantic checks ("the agent actually called the tool") |
| `guidelines` | LLM judge | ~1 LLM call | Quality guidance ("should use MCP tools, not REST") |
| `trace_expectations` | Trace analysis | Free | Tool usage requirements from execution traces |

Start with `expected_facts` and `expected_patterns` (free), then add `assertions` for things that need semantic understanding. See `eval/ground_truth.yaml` in this folder for annotated examples of each type.
