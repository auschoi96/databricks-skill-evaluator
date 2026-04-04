---
name: my-skill
description: "A short description of what your skill does and when to use it. This appears in Claude Code's skill registry and helps the agent decide when to invoke your skill."
---

# My Skill

A brief overview of what this skill enables.

## When to Use This Skill

Use this skill when:
- The user asks to do X
- The user needs help with Y
- The user wants to automate Z

## MCP Tools

| Tool | Purpose |
|------|---------|
| `my_tool_create` | Create a new resource |
| `my_tool_get` | Get or list resources |
| `my_tool_delete` | Delete a resource |

## Quick Start

### 1. Create a Resource

```python
my_tool_create(
    name="My Resource",
    config={"key": "value"}
)
```

### 2. Query a Resource

```python
my_tool_get(resource_id="abc123")
```

### 3. Run a SQL Query

```sql
SELECT * FROM my_catalog.my_schema.my_table
WHERE created_at > '2026-01-01'
LIMIT 10
```

## Common Issues

- **Resource not found**: Verify the resource ID exists by calling `my_tool_get` without an ID to list all resources.
- **Permission denied**: Ensure you have the correct catalog/schema permissions.

## Related Skills

- **[other-skill](../other-skill/SKILL.md)** - Description of how it relates
