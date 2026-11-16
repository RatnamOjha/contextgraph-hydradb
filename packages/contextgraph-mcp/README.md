# ContextGraph MCP adapter

A lightweight, installable stdio MCP server that lets MCP-compatible agents retrieve tool context from ContextGraph. It is a thin client to the existing FastAPI service; it does not bundle Olist data, connect directly to HydraDB, execute returned tools, or call Groq.

## Install and run

Python 3.11 or newer is required.

```bash
python -m venv .venv
. .venv/bin/activate
pip install .
CONTEXTGRAPH_API_URL=https://contextgraph-demo.onrender.com contextgraph-mcp
```

The default API URL is `https://contextgraph-demo.onrender.com`. For a local ContextGraph instance, set `CONTEXTGRAPH_API_URL=http://127.0.0.1:8000`. The stdio process writes MCP protocol messages to stdout; diagnostics go to stderr.

The public demo accepts tasks sent to its API. Use only public/demo information with it. For private tasks, configure an API URL you operate. The adapter does not accept or store API credentials. The current ContextGraph API does not require authentication.

## MCP client configuration

Generic client configuration for an installed package:

```json
{
  "mcpServers": {
    "contextgraph": {
      "command": "contextgraph-mcp",
      "env": {
        "CONTEXTGRAPH_API_URL": "https://contextgraph-demo.onrender.com"
      }
    }
  }
}
```

You can also use an editable local checkout during development:

```json
{
  "mcpServers": {
    "contextgraph": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["-m", "contextgraph_mcp.server"],
      "env": {
        "CONTEXTGRAPH_API_URL": "http://127.0.0.1:8000"
      }
    }
  }
}
```

## Tools

- `retrieve_context(task, order_id?)` returns selected registered tools, schemas, actual dependency relationships, entity resolution, and retrieval provenance. It never executes a tool.
- `inspect_tool(tool_id)` reads the tool catalog and returns schema, logical server, prerequisites, incoming dependency references, and provenance.
- `explain_context(task, order_id?)` explains the current route from returned graph relationships. Saved routing identifiers are not available from the existing API.

The server uses the official Model Context Protocol Python SDK and its stdio transport. Tests cover in-memory protocol calls and a stdio subprocess handshake. The package is not published to PyPI or another registry.
