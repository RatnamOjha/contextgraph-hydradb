# ContextGraph Python SDK

Typed synchronous and asynchronous Python clients for trusted local MCP catalog ingestion, guarded catalog registration, graph-based tool-context retrieval, and labeled retrieval evaluation. The SDK returns metadata and context; your own agent runtime executes tools.

```bash
python -m pip install -e packages/contextgraph-sdk
```

The distribution is not published to PyPI. The command above installs from this repository checkout. For the full isolated backend setup, see `../../docs/SDK_QUICKSTART.md`; for method signatures and request semantics, see `../../docs/SDK_API.md`.

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

For a local backend only, set `CONTEXTGRAPH_ENV=development` and `CONTEXTGRAPH_ENABLE_LOCAL_REGISTRATION=1`. Requests must come from loopback, and the route remains disabled when Render environment markers are present. Registration is an upsert keyed by catalog and tool name; omitted catalog tools are retained (deletion is not supported). Omitted prerequisites preserve an existing dependency set; an explicit empty list clears it; and a nonempty list replaces it after same-catalog validation. Omitted optional metadata preserves existing metadata. MCP refresh omits prerequisites so it cannot erase developer-declared edges. The catalog fingerprint is based on complete effective catalog metadata and dependencies read back from HydraDB. This catalog ID is an isolation boundary in the data model, not production authorization or secure multitenancy.

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

## Retrieve from a developer catalog

```python
context = graph.retrieve(
    task="Check refund eligibility for order 123",
    catalog="company",
    max_tools=8,
)
for tool in context.tools:
    print(tool.id, tool.input_schema)
print(context.dependencies)
print(context.selection_reasons)
```

Custom catalogs use a deterministic lexical rank over tool IDs, names, and descriptions, weighted by how common a term is in that catalog. This is not embedding search. Explicit `DEPENDS_ON` prerequisites are expanded with cycle and size bounds; a missing, excluded, or over-limit prerequisite appears in `missing_dependencies`. Additional candidates omitted by `max_tools` appear in `omitted_tool_ids`; either condition sets `complete=False`. `known_tool_ids` and `exclude_tool_ids` must exactly match IDs in the selected catalog. Known IDs appear as graph context and are omitted from the returned tool definitions; they satisfy a prerequisite only after exact catalog membership validation. Arbitrary values do not satisfy prerequisites. The bounded graph response contains actual stored catalog, server, and dependency relationships. The Olist path remains available with `retrieve(task, order_id=...)` and uses the legacy endpoint.

## Evaluate retrieval labels

`evaluate()` compares graph retrieval with a deterministic flat lexical baseline over the exact same catalog metadata. Each task supplies its required tool IDs. The report measures required-tool recall, retrieved tool count, prerequisite completeness, unrelated tool exposure, estimated schema-token context, and retrieval latency. It does not run tools or an LLM, and it does not measure task completion, model accuracy, billed tokens, or token savings.

```python
report = graph.evaluate("company", [
    {"task": "Check refund eligibility for order 123",
     "required_tools": ["company.commerce.check_refund"]},
], max_tools=8)
print(report.graph_average_required_tool_recall)
print(report.flat_average_estimated_schema_tokens)
```

`inspect_tool()` currently lists the catalog and selects the matching tool because the service has no dedicated inspection endpoint. Network retries are bounded and apply only to idempotent GET requests. The client does not follow redirects.

## Local development

```bash
python -m pip install -e '.[dev]'
pytest
```

All SDK tests use an in-memory HTTP transport; they need neither HydraDB nor provider credentials.
