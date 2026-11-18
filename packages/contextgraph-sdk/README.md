# ContextGraph Python SDK

Typed synchronous and asynchronous Python clients for ContextGraph's current read-only retrieval API.

```bash
python -m pip install -e packages/contextgraph-sdk
```

The distribution is not published to PyPI. The command above installs from this repository checkout.

```python
from contextgraph import ContextGraph

with ContextGraph("http://localhost:8000") as graph:
    print(graph.health())
    tools = graph.list_tools()
    result = graph.retrieve("What is the status of order 123?", order_id="<32-character-olist-order-id>")
    print([tool.id for tool in result.tools])
    print(result.selection_reasons)
```

`AsyncContextGraph` provides the same methods with `await` and an async context manager. The client supports health checks, tool listing and inspection, graph retrieval, and a compact retrieval explanation. Retrieval is read-only: it returns tool metadata and graph relationships; it does not execute a tool or answer a business question. `execution_performed` is always `False`.

## Current API scope

This SDK currently wraps `/api/health`, `/api/tools`, and `/api/graph/explore`; retrieval remains backed by the existing Olist-oriented skill router. Developer tool definitions can be upserted through `register_tools(catalog, tools)` only when the backend is explicitly configured for trusted loopback development. The public demo does not allow this write route. The API does not authenticate SDK callers. `api_key` is an optional bearer header for deployments behind a separately configured gateway; it does not enable authentication on the current demo server. Do not send secrets to a public unauthenticated endpoint.

For a local backend only, set `CONTEXTGRAPH_ENV=development` and `CONTEXTGRAPH_ENABLE_LOCAL_REGISTRATION=1`. Requests must come from loopback, and the route remains disabled when Render environment markers are present. Registration is an upsert keyed by catalog and tool name; refreshes update schema and catalog fingerprint without deleting omitted tools. Explicit prerequisites must name tools in the same registration batch. The server stores catalog and provenance metadata in HydraDB. This workspace ID is an isolation boundary in the data model, not production authorization or secure multitenancy.

## Discover an MCP catalog

`add_mcp_server()` starts the explicitly configured local command, initializes an MCP stdio session, lists tools, then registers definitions. It never calls a business tool. Since this launches a process, only point it at a server you trust.

```python
from contextgraph import ContextGraph

with ContextGraph("http://127.0.0.1:8000") as graph:
    report = graph.add_mcp_server(
        name="commerce",
        command="python",
        args=["commerce_server.py"],
        catalog="company",
    )
    print(report.registered_tool_ids)
```

The async client exposes `await graph.add_mcp_server(...)`. Standard MCP tool metadata does not carry prerequisite relationships, so ingestion does not infer them from descriptions or names. The discovered tool's ID is qualified by server name (for example `company.commerce.get_order`). The developer's agent runtime remains responsible for calling tools and interpreting their results.

`inspect_tool()` currently lists the catalog and selects the matching tool because the service has no dedicated inspection endpoint. Network retries are bounded and apply only to idempotent GET requests. The client does not follow redirects.

## Local development

```bash
python -m pip install -e '.[dev]'
pytest
```

All SDK tests use an in-memory HTTP transport; they need neither HydraDB nor provider credentials.
