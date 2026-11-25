# ContextGraph SDK implementation verification

SDK implementation verification was recorded locally on 2026-11-18. PR review follow-up verification was run locally on 2026-11-25. No Groq calls were made. Catalog writes and MCP integration checks used disposable local HydraDB state. The public Render graph was not touched. The SDK and adapter were not published.

## Phase checks

| Area | Observed result |
| --- | --- |
| Registration/readback | Registered a five-tool unfamiliar catalog in isolated HydraDB. Verified its stored `Catalog.version` and fingerprint directly through HydraDB. Same-state MCP refresh preserved the catalog version. An explicit dependency update changed it; explicit clear changed it again; restoring the edge restored the prior full-catalog fingerprint. |
| MCP dependency preservation | After declaring `factory.check_refund → factory.get_order`, refreshing all tools through MCP metadata ingestion retained the edge and its readback. Explicit `prerequisites: []` cleared it; restoring the dependency restored the exact previous catalog version. The MCP importer now omits prerequisite metadata rather than sending an empty list. |
| Partial registration and fingerprint | A prereq-only registration preserved the existing description and schema. A schema update changed the fingerprint. The other four omitted catalog tools remained registered. Fingerprints are calculated from a full effective-state readback and are insensitive to tool row order. |
| MCP discovery | An independent unfamiliar stdio MCP fixture exposed five tools. The SDK discovered and registered the metadata, then generic retrieval returned task-specific context and an explicit refund-to-order prerequisite. The business tools were not called. |
| Negative control | The unfamiliar fixture's replacement-parts task selected only `ops.factory.list_replacement_parts`; refund/order tools were not added. |
| SDK/MCP adapter parity | For the same task and catalog, the Python SDK and stdio adapter returned the same selected tool IDs and prerequisite edge. Inspection returned the stored prerequisite. |
| Retrieval evaluation | The disposable-graph integration test reported graph required-tool recall of `1.0` for its two labeled fixture tasks. This is retrieval-label recall only, not task success or an LLM result. |
| Registration security | A route-level regression test sent a non-loopback write without local opt-in, observed HTTP 403, and asserted no HydraDB query occurred. |
| Clean installation | Created a new Python virtual environment; editable installs of backend requirements plus `contextgraph-sdk[dev]` and `contextgraph-mcp` completed. `pip check` reported no broken requirements and both packages imported from their source checkouts. This is an install/import check, not a PyPI publication test. |
| Unqualified pytest discovery | Added `pytest.ini` with explicit test roots and asyncio mode. Unqualified pytest now collects and passes the Groq-free suite in the previously failing environment. |
| GitHub CI | A workflow was added to build pinned HydraDB commit `916637477f3f34766b4905b270bd4292109d77e6`, require the binary, and run the isolated graph integration test with the rest of the Groq-free suite. Remote status is pending until the PR follow-up commit is pushed. |

## Test commands and results

The full explicit project, SDK, adapter, and disposable-HydraDB test paths passed:

```text
env -u GROQ_API_KEY python -m pytest -q tests packages/contextgraph-sdk/tests packages/contextgraph-mcp/tests --ignore=tests/test_groq_integration.py
76 passed in 6.30s (fresh virtual environment; 2026-11-25)
```

The focused SDK registration and HydraDB/MCP integration set also passed:

```text
env -u GROQ_API_KEY python -m pytest -q --ignore=tests/test_groq_integration.py
76 passed in 6.79s (unqualified collection; 2026-11-25)
```

All external MCP fixtures used in these checks ran locally. The HydraDB end-to-end test starts an isolated process and uses temporary storage. The Groq integration test was excluded and `GROQ_API_KEY` was unset. Clean-venv `pip check` returned `No broken requirements found`; imports resolved to the editable source trees.

## Test runner note

Before adding `pytest.ini`, running `pytest` with no explicit paths from the repository root triggered a collection-time `pytest-asyncio` internal error (`AttributeError: 'Package' object has no attribute 'obj'`) in the preinstalled environment. With explicit test roots in `pytest.ini`, unqualified collection now passes. The local global Python environment still has unrelated dependency conflicts; package consistency was checked in a fresh virtual environment, where `pip check` passed. CI also creates an isolated dependency environment.

## Limits of the evidence

These are local implementation checks against a small fictional catalog. They do not establish production security, secure multitenancy, customer adoption, semantic retrieval quality, business-task completion, billed token savings, or a working hosted developer-catalog service. The SDK uses exact declared prerequisites and deterministic lexical ranking. The evaluator's labels are supplied by the caller and token counts are estimates.
