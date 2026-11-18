# ContextGraph Python SDK

Typed synchronous and asynchronous Python clients for ContextGraph's current read-only retrieval API.

```bash
pip install contextgraph-sdk
```

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

This SDK currently wraps the ContextGraph demo's `/api/health`, `/api/tools`, and `/api/graph/explore` endpoints. Retrieval is backed by the existing Olist-oriented skill router and optional Olist order identifiers. The API does not yet register arbitrary developer catalogs, offer a single-tool endpoint, or authenticate SDK callers. `api_key` is an optional bearer header for deployments behind a separately configured gateway; it does not enable authentication on the current demo server. Do not send secrets to a public unauthenticated endpoint.

`inspect_tool()` currently lists the catalog and selects the matching tool because the service has no dedicated inspection endpoint. Network retries are bounded and apply only to idempotent GET requests. The client does not follow redirects.

## Local development

```bash
python -m pip install -e '.[dev]'
pytest
```

All SDK tests use an in-memory HTTP transport; they need neither HydraDB nor provider credentials.
