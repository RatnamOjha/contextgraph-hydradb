# ContextGraph SDK: Architecture and endpoint contract

**Status:** Phase 0 audit, 2026-11-18, repository commit `a8b520a`. Local `HEAD`, `origin/main`, and GitHub `main` matched during the audit. This document describes the current repository and the proposed SDK MVP. Proposed APIs below are contracts for later phases; they are not implemented yet. No routing behavior is changed in this phase.

## 1. Current architecture

The repository is a single FastAPI application plus a separately installable MCP adapter. The browser UI is served from `web/`. The API queries HydraDB over its authenticated HTTP JSON query endpoint through `backend.hydra.HydraClient`. Olist business facts are read from the bundled SQLite subset through `backend.olist_store`; HydraDB stores catalog metadata and anonymized entity relationships, not the order facts themselves.

The current retrieval path is:

1. `backend.router.candidate_skills()` applies deterministic lexical term matching against the built-in skill vocabulary.
2. For the Olist path, `route_graph_candidates()` queries candidate skill-to-tool edges, then follows `DEPENDS_ON` edges in bounded, cycle-safe client-side waves. A requested Olist order can add an entity path.
3. `/api/graph/explore` serializes that actual route as a bounded subgraph for Graph Explorer and the MCP adapter. It does not execute tools.
4. `/api/run` uses the retrieved catalog for either deterministic fixture execution or the separate Groq agent path. The evaluator is a distinct step.

`/api/graph` is an older fixture route keyed by a fixed scenario. It is not a generic task router. The lexical matcher, required-tool checks, deterministic executor, Olist entity parser, and dataset workflow are demo-specific. The current application does not accept arbitrary developer-owned catalogs.

### Reusable models and logic

- `backend.fixtures.Tool`: name/key, description, server, skill, JSON input schema, declared dependencies, and entity kinds. Its schema is currently stored as a JSON string.
- `backend.router.tool_json()`: serializes the tool metadata used in context-token estimates.
- `route_graph_candidates()`: bounded HydraDB skill/tool/dependency retrieval, including optional Olist entity resolution.
- `/api/tools`: reads registered tool metadata.
- `/api/graph/explore`: returns selected tools and relationships with per-tool metadata and selection reasons.
- The `Execution` and objective models are useful to the existing Playground, but execution and objective success are outside the SDK's retrieval responsibility.

### Existing read-only endpoint contract

| Endpoint | Current behavior | SDK use / limitation |
|---|---|---|
| `GET /api/health` | Probes the built-in agent node in HydraDB; returns `ok`, `hydradb`, and `probe_ms`. | Suitable for `health()`. It currently assumes the demo seed exists. |
| `GET /api/tools` | Returns `{ "tools": [...] }` with key, name, description, server, schema, skill, execution status, source, and dependencies. | Suitable for `list_tools()` against the default demo catalog. There is no catalog selector or single-tool endpoint. |
| `GET /api/graph/explore?task=...&order_id=...` | Returns task, optional Olist order ID, candidate skills, bounded nodes/edges, resolved entities, latency, and caps. Tool nodes include schemas, dependencies, provenance, and `why`. It is read-only. | Suitable for an initial SDK `retrieve()`/`explain()` against the demo route. It currently restricts order IDs to Olist's 32-character hex format and ranks with built-in demo skills. |
| `GET /api/graph?scenario=...` | Returns a graph for one of the fixed demo scenarios. | Not a general SDK retrieval endpoint. |

### Developer catalog registration status

Phase 2 adds `POST /api/v1/catalogs/{catalog}/tools` and SDK `register_tools(catalog, tools)`. It is disabled by default and rejects non-loopback callers, Render runtimes, and requests unless both `CONTEXTGRAPH_ENV=development` and `CONTEXTGRAPH_ENABLE_LOCAL_REGISTRATION=1` are explicitly set. It creates catalog-scoped tool IDs, stores schemas/provenance/fingerprints in HydraDB, and persists only caller-declared prerequisites. Registration is an idempotent upsert; omitted tools remain in the catalog. This is a local development capability, not public catalog ingestion or secure multitenancy. Phase 4 adds deterministic lexical retrieval for these catalogs.

Phase 4 adds `POST /api/v1/catalogs/{catalog}/retrieve` and SDK `retrieve(task, catalog=..., max_tools=...)`. It uses bounded HydraDB reads scoped to one catalog, exact lexical metadata overlap with catalog rarity weights, and client-side cycle-safe prerequisite expansion. It does not execute tools, call an LLM, use embeddings, or claim that arbitrary values satisfy prerequisites. A missing or excluded prerequisite marks the response incomplete. `known_tool_ids` are accepted only after exact catalog membership validation. The legacy Olist retrieval remains on `/api/graph/explore`.

Phase 6 refactors `packages/contextgraph-mcp` to use `AsyncContextGraph` for tool listing, inspection, Olist retrieval, and catalog retrieval. It keeps the existing MCP tool names and return fields while adding optional catalog controls. A disposable HydraDB integration test compares SDK and MCP retrieval outputs for the same task and catalog.

At the Phase 0 audit, no endpoint registered tools or refreshed a catalog. There is still no `GET /api/tools/{id}` endpoint. The MCP adapter now calls the same typed SDK client as Python applications; SDK `inspect_tool()` still lists the catalog because the API has no single-tool endpoint.

## 2. HydraDB graph model and write boundaries

The built-in seed creates `Agent`, `TaskType`, `Skill`, `Tool`, `MCPServer`, and `Entity` nodes. Catalog edges include `HAS_SKILL`, `REQUIRES`, `USES`, `PROVIDED_BY`, and explicit `DEPENDS_ON`. Olist edges include `HAS_CONTEXT`, `IDENTIFIES`, `HAS_ORDER`, `BELONGS_TO`, `HAS_ITEM`, `ITEM_PRODUCT`, and `SOLD_BY`. IDs are deterministic hashes from node kind and key. Dependency traversal runs as bounded application-side query waves; this is not presented as a database-native shortest-path operation.

Write paths that matter to SDK design:

- `POST /api/seed` defaults to `reset=true`. `seed_catalog()` uses `DETACH DELETE` on the demo labels, including `Tool`, `Entity`, `Execution`, and `LearningUpdate`, before rebuilding the demo. It must not be used to onboard a developer catalog.
- Catalog seeding creates/merges nodes but creates relationships. Repeating a non-reset seed can therefore duplicate edges.
- `/api/run` may append `Execution` and relationship records. `/api/learning/*` may store learning evidence and rollback state. Those flows are not SDK retrieval calls.
- The hosted entrypoint seeds on startup; the public service has volatile storage. SDK tests and future writes must use an isolated local HydraDB instance and scoped, non-destructive upserts.

The new catalog model must use stable catalog-qualified identities and explicit `DEPENDS_ON` metadata supplied by the developer. Similar tool names are not evidence that one tool is a prerequisite for another.

## 3. Proposed SDK responsibilities

The Python SDK is a typed client and local MCP catalog-discovery helper. It should:

- Call health, list, inspect, retrieve, explain, register, and evaluation APIs through a shared sync/async HTTP transport.
- Validate and normalize responses into public models.
- In stdio MCP ingestion, launch only a command explicitly supplied by the developer, list tools, close the MCP session, and send only discovered metadata to ContextGraph. It must never invoke the discovered business tools.
- Return schemas, declared prerequisites, retrieval reasons, source/provenance, timing, and `execution_performed=False`.
- Raise actionable errors for connection, HTTP, and response-shape failures without echoing credentials.

The SDK must not import `backend.*`, execute tools, call an LLM, infer dependencies, or claim that retrieval alone means the task was completed.

## 4. Proposed backend responsibilities

The backend owns catalog validation, workspace/catalog scoping, HydraDB reads/writes, dependency completeness, bounded deterministic lexical ranking, retrieval explanations, and evaluations over supplied task specifications. It does not own the developer's agent loop or tool execution.

### Proposed MVP endpoint contract

These `/api/v1` endpoints are **proposed**, not present in the current application:

#### `GET /api/v1/health`

A general service/HydraDB readiness response without requiring the Olist demo seed:

```json
{"ok": true, "hydradb": "connected", "probe_ms": 2.4}
```

#### `GET /api/v1/catalogs/{catalog_id}/tools`

List only the requested catalog's tools. Each item contains stable ID, public name, description, JSON input schema, optional output schema, server identity, provenance, version/fingerprint, and explicitly declared prerequisites.

#### `GET /api/v1/catalogs/{catalog_id}/tools/{tool_id}`

Return one tool and its direct incoming/outgoing prerequisite relationships. Return 404 when the tool is not in the selected catalog.

#### `PUT /api/v1/catalogs/{catalog_id}/tools`

Idempotent catalog upsert/refresh. Example request:

```json
{
  "server": "commerce",
  "version": "sha256:catalog-fingerprint",
  "tools": [
    {
      "name": "get_order",
      "description": "Retrieve one order",
      "input_schema": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]},
      "output_schema": null,
      "dependencies": []
    }
  ]
}
```

The backend validates JSON schemas, upserts only IDs within the selected catalog, removes stale catalog-owned nodes/edges on refresh without touching demo or other catalogs, and reports per-tool outcomes. The API must reject this mutation unless an explicit trusted-local configuration is enabled. This gate is not a substitute for production authentication.

#### `GET /api/v1/catalogs/{catalog_id}/context?task=...`

Read-only graph retrieval. Optional query parameters: `max_tools` (bounded), repeated `known_tool` values (only validated IDs in this catalog), and repeated `exclude_tool` values. It ranks from that catalog only, expands explicit prerequisites transitively with cycle protection, reports missing prerequisites, and never falls back to Olist fixtures.

```json
{
  "task": "Check refund eligibility for order 123",
  "catalog": "commerce",
  "tools": [
    {"id": "commerce.get_order", "name": "get_order", "input_schema": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]}, "provenance": "mcp:commerce"}
  ],
  "dependencies": [],
  "explanation": [{"tool_id": "commerce.get_order", "reason": "Matched task terms: order"}],
  "missing_dependencies": [],
  "complete": true,
  "retrieval_ms": 3.1,
  "execution_performed": false
}
```

The SDK's `retrieve()` and `explain()` use this same response so both interfaces have consistent semantics. `inspect_tool()` maps to the single-tool route. Health and list methods use the matching read endpoints. Retrieval must be stable for identical catalog/task inputs and clearly label lexical ranking as lexical.

#### `POST /api/v1/catalogs/{catalog_id}/evaluate`

Accept explicit tasks and required-tool IDs, return retrieval-only recall, dependency completeness, result size, unrelated-tool exposure, estimated schema context, and latency. It must not claim agent task completion, actual LLM usage, or billed token savings.

## 5. Local development workflow

The current local entrypoint is `./scripts/dev.sh`. It starts the local HydraDB binary and FastAPI, then seeds the demo catalog and runs deterministic evaluation. The script defaults HydraDB to its in-memory provider; set `CONTEXTGRAPH_HYDRA_PROVIDER=local` to try the local filesystem provider. Its default seed uses `reset=true`, so it is a demo bootstrap, not a reusable SDK catalog command.

For SDK implementation, use a separate state directory, in-memory HydraDB, and alternate ports where needed. Seed/reset only that isolated graph. A future external-catalog integration test should start from empty isolated storage, ingest an unfamiliar MCP fixture, close/restart the API/graph against that same isolated store, and read the catalog back. Never use the public Render graph for mutation tests. Existing `data/olist_demo.sqlite` is bundled solely for the Olist demo.

The root `requirements.txt` installs FastAPI, Uvicorn, httpx, and Pydantic. There is no root packaging configuration. The Docker image includes only the backend, web assets, SQLite demo file, and Render entrypoint; the separately packaged MCP adapter is not part of the service image. Render uses one free Docker web service and configures the Groq key as a non-synced secret. Its entrypoint starts HydraDB, seeds with reset enabled, runs deterministic evaluation, and then serves the API. Tests live under `tests/` and `packages/contextgraph-mcp/tests/`; `test_groq_integration.py` makes a real provider call when `GROQ_API_KEY` is configured, so local SDK verification must deliberately disable or skip that test to honor the no-Groq requirement.

## 6. Security boundaries

- The current FastAPI API does not enforce authentication. The SDK and MCP adapter send unauthenticated requests by default; `api_key` is only a header pass-through for a separately configured gateway.
- An SDK `api_key` argument must not be advertised as access control until a backend middleware actually validates it. It may be reserved or rejected/documented as unused until then.
- The Render demo is public and must not receive developer-private tool metadata. Keep catalog writes disabled there.
- A workspace/catalog identifier scopes queries but does not provide secure multitenancy by itself. Authorization must bind authenticated principals to allowed catalogs before public writes are enabled.
- A stdio MCP command executes local code at startup. The SDK may run only commands and arguments explicitly trusted and supplied by the caller. It discovers tool metadata only; it does not call business tools.
- Schemas, descriptions, tool names, and MCP server output are untrusted metadata. Validate size/shape and HTML/JSON rendering boundaries.
- HydraDB credentials remain backend configuration and must not appear in SDK responses, logs, or exceptions.

## 7. MVP limits

- Current retrieval is built-in/demo-specific, mostly lexical, and uses fixed skill/scenario metadata. It is not embedding search or autonomous dependency learning.
- Current generic registration and workspace authorization do not exist.
- MCP ingestion is not implemented; the existing MCP package is a read-only adapter to this API, not a server-catalog importer.
- Current graph retrieval responses do not execute tools and do not prove that an external agent can complete a task.
- Context token numbers in current reports estimate serialized tool-schema context; they are not provider billing measurements.
- The MVP should support explicit metadata and a small bounded catalog. It should report missing prerequisite nodes rather than silently dropping or inventing them.
