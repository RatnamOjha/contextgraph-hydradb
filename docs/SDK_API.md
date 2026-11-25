# ContextGraph Python SDK API

Install the package from a local checkout (the distribution is not published):

```bash
python -m pip install -e packages/contextgraph-sdk
```

```python
from contextgraph import ContextGraph

with ContextGraph("http://127.0.0.1:8001") as cg:
    report = cg.add_mcp_server(
        name="commerce",
        command="python",
        args=["examples/commerce_mcp_server.py"],
        catalog="dummy-company",
    )
    # Add only relationships confirmed by the developer. MCP metadata
    # itself does not declare tool prerequisites.
    cg.register_tools("dummy-company", [{
        "name": "commerce.check_refund",
        "server": "commerce",
        "description": "Check refund eligibility for an order",
        "input_schema": {"type": "object", "properties": {
            "order_id": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["order_id", "reason"]},
        "prerequisites": ["commerce.get_order"],
    }])
    context = cg.retrieve(
        "Check refund eligibility for order 123",
        catalog="dummy-company",
        max_tools=8,
    )
    print(report.registered_tool_ids)
    print([tool.id for tool in context.tools])
    print(context.dependencies)
    print(context.selection_reasons)
```

## Client methods

`ContextGraph(url, timeout=15.0, api_key=None, retries=1)` is synchronous and supports `close()` and a context manager. `AsyncContextGraph` provides async versions and an async context manager. The optional `api_key` is sent as a bearer header; the current ContextGraph API does not validate it. Use it only behind a gateway that does.

| Method | Behavior |
| --- | --- |
| `health()` | Read `/api/health`. |
| `list_tools()` | Read `/api/tools` and normalize tool definitions. The API list is not catalog-filtered. |
| `inspect_tool(tool_id)` | Find a listed tool by exact ID; currently implemented by listing and selecting because the API has no inspect-by-ID route. |
| `register_tools(catalog, tools)` | POST explicit tool metadata to a trusted local catalog. |
| `add_mcp_server(name, command, args, catalog, env, cwd, timeout)` | Launch a trusted local stdio MCP server, list tools, register metadata, and return `MCPIngestionReport`. Does not invoke business tools. |
| `retrieve(task, catalog=..., max_tools=..., known_tool_ids=..., exclude_tool_ids=...)` | Retrieve bounded graph context from a developer catalog. |
| `retrieve(task, order_id=...)` | Use the legacy Olist demo retrieval path. Do not pass catalog-specific options without a catalog. |
| `explain(...)` | Return selected IDs, reasons, dependencies, omissions, and completeness from the same retrieval result. |
| `evaluate(catalog, tasks, max_tools=...)` | Locally compare graph retrieval with a flat lexical baseline on caller-labeled required tools. |

## Registration schema

Each registration definition has:

- `name` (required stable identifier), `description` (optional, up to 4,000 characters)
- `input_schema` and `output_schema` (optional JSON objects, each pair bounded to 64 KiB)
- `server` (optional stable identifier; defaults to the catalog)
- `prerequisites` (optional list of same-catalog tool names, default empty)

Catalogs accept up to 200 definitions per registration call. A tool's stable ID is `{catalog}.{name}`. Prerequisites may refer to a tool in this call or one already registered in the same catalog. Invalid or unknown prerequisites reject the registration. For each tool, omitted `prerequisites` preserve its current dependency edges, while `prerequisites: []` explicitly clears them and a nonempty list replaces them. Omitted optional metadata fields preserve their stored values on an existing tool; explicit `null` schemas clear those schema values. Newly registered tools start without dependencies unless prerequisites are supplied. Omitted catalog tools remain stored; deletion is not supported by this API. The registration API is disabled unless the local trusted-development guard is configured.

## Retrieval result

`RetrievalResult` contains `task`, `tools`, `dependencies`, `selection_reasons`, `missing_dependencies`, `omitted_tool_ids`, `known_tool_ids`, `complete`, `retrieval_ms`, `source`, and `metadata`. `execution_performed` is a literal `False`. Each `ToolDefinition` includes its schema, server, catalog, provenance, and declared dependencies when available.

For generic catalogs, `known_tool_ids` and `exclude_tool_ids` must match exact IDs in the selected catalog. Known tools are represented as graph context, omitted from returned definitions, and satisfy prerequisites only after membership validation. Exclusions and result limits can leave prerequisites missing; in that case the response marks incomplete rather than claiming a complete tool set.

## Evaluation task shape

```python
report = cg.evaluate("dummy-company", [
    {"task": "Find the delivery status of an order",
     "required_tools": ["dummy-company.get_order"]},
])
```

Task labels are caller-provided assumptions, not learned ground truth. See [evaluation methodology](SDK_EVALUATION.md) for exact metrics and their limits.
