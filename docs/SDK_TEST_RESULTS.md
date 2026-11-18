# ContextGraph SDK implementation verification

Verification recorded locally at `bf9bd35` on 2026-11-18. No Groq calls were made. Catalog writes and MCP integration checks used disposable local HydraDB state. The public Render graph was not touched. The SDK and adapter were not published.

## Phase checks

| Area | Observed result |
| --- | --- |
| Registration/readback | Registered two catalog tools, restarted the API against the isolated graph, and read the catalog back. Repeating registration kept stable IDs and dependency edges. A later refresh updated supplied schema and removed an omitted schema property without deleting omitted catalog tools. |
| MCP discovery | An independent unfamiliar stdio MCP fixture exposed five tools. The SDK discovered and registered the metadata, then generic retrieval returned task-specific context and an explicit refund-to-order prerequisite. The business tools were not called. |
| Negative control | The unfamiliar fixture's replacement-parts task selected only `ops.factory.list_replacement_parts`; refund/order tools were not added. |
| SDK/MCP adapter parity | For the same task and catalog, the Python SDK and stdio adapter returned the same selected tool IDs and prerequisite edge. Inspection returned the stored prerequisite. |
| Retrieval evaluation | The disposable-graph integration test reported graph required-tool recall of `1.0` for its two labeled fixture tasks. This is retrieval-label recall only, not task success or an LLM result. |
| Registration security | A route-level regression test sent a non-loopback write without local opt-in, observed HTTP 403, and asserted no HydraDB query occurred. |
| Clean installation | Created a new Python virtual environment; editable installs of `contextgraph-sdk[dev]` and `contextgraph-mcp` completed. `pip check` reported no broken requirements and both packages imported from their source checkouts. This is an install/import check, not a PyPI publication test. |

## Test commands and results

The full explicit project, SDK, adapter, and disposable-HydraDB test paths passed:

```text
env -u GROQ_API_KEY python -m pytest -q tests packages/contextgraph-sdk/tests packages/contextgraph-mcp/tests --ignore=tests/test_groq_integration.py
74 passed in 7.28s
```

The focused SDK registration and HydraDB/MCP integration set also passed:

```text
env -u GROQ_API_KEY python -m pytest -q tests/test_developer_catalog.py tests/test_sdk_catalog_integration.py packages/contextgraph-sdk/tests packages/contextgraph-mcp/tests
24 passed in 5.64s
```

All external MCP fixtures used in these checks ran locally. The HydraDB end-to-end test starts an isolated process and uses temporary storage. The Groq integration test was excluded and `GROQ_API_KEY` was unset.

## Test runner note

Running `pytest` with no explicit paths from the repository root triggered a collection-time `pytest-asyncio` internal error (`AttributeError: 'Package' object has no attribute 'obj'`) in the preinstalled environment before any test ran. Explicitly naming the project test directories avoided that legacy plugin collection path and the complete intended suite passed. This environment-specific broad-discovery issue is not presented as a passing test.

## Limits of the evidence

These are local implementation checks against a small fictional catalog. They do not establish production security, secure multitenancy, customer adoption, semantic retrieval quality, business-task completion, billed token savings, or a working hosted developer-catalog service. The SDK uses exact declared prerequisites and deterministic lexical ranking. The evaluator's labels are supplied by the caller and token counts are estimates.
