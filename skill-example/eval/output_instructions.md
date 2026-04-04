# Output Evaluation Criteria for my-skill

These criteria tell the LLM judge what correct output looks like for your
skill's tasks. Define the expected artifacts, mandatory facts, and quality
expectations.

## Expected Artifacts

### Create tasks
- The resource should be created (verifiable via a follow-up `my_tool_get` call)
- The resource should have the correct configuration applied
- Response should include the resource ID for user reference

### Query tasks
- Response should include the resource details
- Results should be presented in a readable format (table, list, or structured)

### Delete tasks
- Response should confirm the deletion was successful
- Response should warn the user if the operation is irreversible

## Mandatory Facts
These are defined per test case in `ground_truth.yaml` under
`expectations.expected_facts`. The semantic grader checks these using a
3-phase pipeline:
1. Deterministic substring matching (zero cost)
2. Agent-based grading with execution transcript
3. LLM semantic fallback for missed substring matches

## WITH vs WITHOUT Comparison
Each test case is run both WITH and WITHOUT the skill. Assertions are classified:
- **POSITIVE**: Skill helped the agent produce correct output
- **REGRESSION**: Skill confused the agent (output was better without it)
- **NEEDS_SKILL**: Neither response covers this — skill must teach it
- **NEUTRAL**: Agent already knows this without the skill
