# ContextGraph SDK architecture

**Implementation status:** SDK MVP implemented locally through Phase 8 at `bf9bd35`. This document records the delivered design; the initial audit is preserved in Git history. The packages are not published and the branch has not been pushed.

## Product boundary

ContextGraph is a tool-context retrieval layer for developers building tool-using agents. A developer's runtime remains responsible for model calls, argument selection, tool execution, and interpreting results. ContextGraph discovers explicitly trusted MCP metadata, stores catalog definitions and declared prerequisite relationships in HydraDB, retrieves a bounded task-specific context, and explains that selection. SDK retrieval never executes tools or calls an LLM.

## Architecture

```text
Developer agent/runtime ── Python SDK ── FastAPI ── HydraDB
MCP client ── ContextGraph MCP adapter ── Python SDK ── FastAPI
                                      └── Olist SQLite data (demo execution only)
```

- `backend/app.py` serves the dashboard and API. Existing Olist routes and execution workflows remain intact.
- `backend/developer_catalog.py` validates catalog metadata and performs catalog-scoped, non-destructive upserts. Prerequisites are explicit.
- `backend/catalog_retrieval.py` ranks tools lexically and expands declared dependencies in bounded, cycle-safe application-side traversal.
- `packages/contextgraph-sdk` provides typed sync/async HTTP clients, trusted local stdio MCP discovery, retrieval explanations, and labeled retrieval evaluation.
- `packages/contextgraph-mcp` reuses the SDK to expose retrieval and inspection to MCP clients.
- HydraDB stores tools, catalogs, MCP servers, and their relationships. Olist business facts remain in the bundled SQLite subset.

## Implemented HTTP contract

| Method and route | Purpose | Boundary |
| --- | --- | --- |
| `GET /api/health` | API and HydraDB probe | Read-only |
| `GET /api/tools` | List built-in and registered tool metadata | Read-only; not catalog-filtered at the HTTP layer |
| `POST /api/v1/catalogs/{catalog}/tools` | Register or refresh supplied metadata | Loopback-only, explicit local opt-in, disabled on Render |
| `POST /api/v1/catalogs/{catalog}/retrieve` | Bounded catalog retrieval | Read-only; no fallback to Olist |
| `GET /api/graph/explore` | Existing Olist task/entity context route | Read-only, Olist-oriented |

`ContextGraph.evaluate()` currently runs locally in the SDK. It calls catalog retrieval and compares those results against a flat lexical top-k baseline using the same listed catalog tools. There is no evaluation HTTP route.

## Catalog and retrieval semantics

Tool IDs are catalog-qualified. Registration is an idempotent upsert for supplied definitions. It updates explicitly supplied schemas and replaces the supplied tools' `DEPENDS_ON` edges. It does not delete omitted tools when a catalog refresh omits them. Prerequisites must resolve to a tool in the current batch or the same catalog already stored in HydraDB; relationships are never inferred from names or descriptions.

Retrieval searches names, IDs, and descriptions using deterministic lexical token matching with catalog term-frequency weighting. It returns selected schemas, actual catalog/server/dependency graph relationships, selection reasons, omitted and missing dependencies, completeness, and retrieval timing. Dependency expansion is bounded, cycle-safe application-side traversal over queried graph edges. This is not semantic/embedding search or a database-native path algorithm. Retrieval returns context; it does not establish that an agent completed the requested task.

The Olist route is retained for the hackathon demo and can optionally resolve a supplied historical order ID. It is separate from generic developer catalog retrieval. The synthetic-company benchmark is a local deterministic comparison fixture, not a customer environment.

## Local development

Use `./scripts/sdk-dev.sh` to start an isolated developer-catalog backend on port 8001. It uses `.hydradb-sdk/`, does not seed/reset the existing demo graph, and enables registration only for loopback requests. The standard `./scripts/dev.sh` continues to run the Olist demo and has its own graph state. Never use `/api/seed` to onboard developer tools: the demo seed can reset demo labels.

The end-to-end fixture `examples/commerce_mcp_server.py` exposes five fictional tools. `examples/basic_retrieval.py` discovers those metadata definitions, explicitly registers a refund prerequisite, and retrieves context from HydraDB. Integration tests start a disposable HydraDB process and use a temporary state directory. The hosted Render graph is not used for SDK write tests.

## Security and production boundary

Developer-catalog writes require `CONTEXTGRAPH_ENV=development`, `CONTEXTGRAPH_ENABLE_LOCAL_REGISTRATION=1`, and a loopback caller. The route is disabled when Render environment markers are present. This is a local-development guard, not authentication. Catalog IDs are data scoping, not secure multitenancy. The current API has no user authorization; the SDK's optional bearer header is only useful behind a separately configured gateway that validates it.

`add_mcp_server()` launches the exact local command supplied by the developer. Treat that command as trusted code. The SDK initializes the MCP session and lists tool metadata, then closes it; it does not invoke business tools. Metadata and schemas are validated and bounded, but callers remain responsible for trusting their source.

Do not expose private catalogs through the public demo or enable catalog writes on a shared service. A production product needs authenticated catalog ownership, authorization, safe secret handling, and durable storage before hosted private ingestion.

## Explicit MVP limits

- No authentication or secure multitenancy; catalog IDs alone do not grant access control.
- No PyPI publication; install from this checkout.
- Lexical retrieval only; no embeddings or LLM reranking.
- Dependencies are developer-declared; discovery does not guess them.
- No tool execution, model orchestration, or claim of task completion in the SDK.
- Evaluation uses supplied labels and an approximate `serialized characters / 4` schema-token estimate. It does not measure actual model tokens, billing, agent accuracy, or business outcomes.
- The SDK MCP helper supports local stdio servers only; it does not provide arbitrary remote MCP transport.
- Catalog refresh updates supplied tools but does not prune omitted tools.
- Render's demo storage is ephemeral; no durable hosted developer catalog service is configured.
