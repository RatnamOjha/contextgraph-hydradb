# ContextGraph SDK troubleshooting

## The SDK backend does not start

Run `./scripts/sdk-dev.sh` from the repository root. The first run builds the local HydraDB binary; Rust and the upstream HydraDB build prerequisites are needed. Check that ports `8001`, `18444`, `17688`, and `19092` are available. The SDK backend uses `.hydradb-sdk/`, separate from the Olist demo state.

If HydraDB is reachable but the API is not, check the startup output and try `curl -fsS http://127.0.0.1:8001/api/health`. Do not run the demo seed to repair an SDK workspace: it targets the Olist/demo graph.

## Registration returns HTTP 403

The developer write route is enabled only by `scripts/sdk-dev.sh`. Confirm the API process was started by that script and the request uses `127.0.0.1` or `localhost`. The route intentionally stays disabled in Render and other non-development environments. Do not enable this unauthenticated route on a shared host.

## Registration rejects a prerequisite

`prerequisites` must name a tool in the same catalog, either in the current registration batch or already stored there. Use the stable registered tool name, not a display label. Cross-catalog, inferred, misspelled, or missing relationships are rejected. Standard MCP tool listings do not declare prerequisites, so register confirmed relationships explicitly.

## Retrieval says a tool or catalog is missing

The generic retrieval method requires the exact catalog name used during ingestion:

```python
cg.retrieve("Check refund eligibility for order 123", catalog="dummy-company")
```

Check `MCPIngestionReport.registered_tool_ids`, the catalog identifier, and the API response. Generic catalog retrieval does not fall back to Olist tools. Olist retrieval is a separate legacy path and uses `order_id=` instead of `catalog=`.

## Retrieval is incomplete or too broad

Inspect `missing_dependencies`, `omitted_tool_ids`, `selection_reasons`, and `complete`. Increase `max_tools` within its limit if required tools were clipped; verify that dependencies were explicitly registered and not excluded. Lexical ranking uses names, IDs, and descriptions, so improve accurate metadata when terms do not overlap. It is not semantic search.

## MCP startup errors or timeouts

Run the configured command manually from the same working directory. Confirm the program speaks MCP over stdio and writes protocol output to stdout rather than ordinary logs. Only trusted commands should be configured. Raise `timeout` for a legitimately slow local startup; discovery has bounded pagination and does not call business tools.

## An editable install cannot import `contextgraph`

From the repository root, activate the intended virtual environment and run:

```bash
python -m pip install -e packages/contextgraph-sdk
python -c 'import contextgraph; print(contextgraph.__file__)'
```

The project distribution is not on PyPI. For the ContextGraph MCP adapter, install both local projects as shown in `packages/contextgraph-mcp/README.md`.
