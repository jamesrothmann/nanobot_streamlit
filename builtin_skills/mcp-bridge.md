---
name: mcp-bridge
description: Bridge to external MCP-like tool servers over HTTP. Use when specialized remote tools are configured in secrets and should be callable from the agent.
---

# MCP Bridge

Use these tools:

1. `mcp_list_servers()` to discover configured endpoints.
2. `mcp_call(server, tool_name, arguments_json)` to invoke remote tools.

## Configuration

Define servers in secrets:

- `[mcp_servers.<name>]`
- `url = "https://..."`
- `token = "..."` (optional bearer token)

