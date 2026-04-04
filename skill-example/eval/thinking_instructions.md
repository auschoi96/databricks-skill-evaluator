# Thinking Evaluation Criteria for my-skill

These criteria tell the LLM judge what "good reasoning" looks like when an agent
uses your skill. Focus on efficiency, tool selection, error recovery, and
completeness.

## Efficiency
- A simple creation task should take 1-3 tool calls
  - Optimal: `my_tool_create` directly (1 call)
  - Acceptable: inspect first, then create (2 calls)
  - Excessive: More than 5 tool calls for a simple task
- Agent should NOT write raw Python/curl to call REST APIs when MCP tools exist
- Agent should NOT use Bash to invoke CLIs when MCP tools cover the operation

## Tool Selection
- For creating resources: MUST use `my_tool_create` MCP tool
- For querying resources: MUST use `my_tool_get` MCP tool
- For deleting resources: MUST use `my_tool_delete` MCP tool

## Recovery
- If a tool call fails with an invalid input, the agent should:
  1. Diagnose what went wrong (check the error message)
  2. Inform the user which input is problematic
  3. NOT retry the same call with the same bad parameters
- If a resource is not found, list available resources to help the user

## Completeness
- For creation tasks: MUST actually invoke the tool (not just describe how to)
- For query tasks: MUST present results in a readable format
- For multi-step tasks: MUST follow a logical order (inspect -> create -> verify)
