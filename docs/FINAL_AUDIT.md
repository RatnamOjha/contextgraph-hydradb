# Final engineering audit

## Delivered

- HydraDB remains the live graph for task/skill/tool relationships, transitive tool prerequisites, Olist order/customer/item/product/seller entity links, and execution evidence. SQLite answers minimized historical business-data queries.
- The deterministic fixture path runs against source rows and remains reproducible. A separate Groq agent path sends only graph-retrieved Olist tools, validates model arguments and dependencies, executes calls against SQLite, and produces an answer. A real Groq integration test completed successfully with one bounded case; credentials are kept in the local ignored `.env` and were not added to Render or Git.
- The order ID is parsed from each request. Both the relational record and matching HydraDB entity path must exist for graph mode; unknown IDs fail closed.
- The importer creates a deterministic 500-order subset and omits customer location, reviews, free text, and geolocation. The source archive remains local and gitignored; the curated SQLite subset is bundled with the app image.
- The deterministic held-out suite has nine separate cases, including unfamiliar phrasing and overlapping tool descriptions. It compares full context, flat lexical retrieval, dependency-expanded flat, semantic skill-candidate plus dependencies, graph retrieval with learning updates excluded, and HydraDB graph retrieval. Per-case expected tools, selected tools, entity match, and execution outcomes are returned. No model calls are made unless the optional real-agent subset is requested.
- The learning demonstration removes an actual declared prerequisite, records the failed execution, validates the missing dependency against tool metadata, applies a HydraDB relationship update, retests on a distinct order, and exposes rollback.
- The interface now has a clear Playground, Learning Loop, and Tool Registry, larger typography, explicit fixture/Groq states, selected order IDs, tool provenance/dependency details, execution trace, and held-out per-case results.

## Local verification

- `python -m compileall -q backend scripts tests` passed.
- `tests/test_core.py`: five tests passed.
- `tests/test_olist_store.py`: two tests passed.
- `tests/test_groq_integration.py`: one live Groq test passed against a graph-selected tool and actual Olist order.
- Local API smoke checks returned distinct records for distinct order IDs and failed closed for an unknown order ID.
- The HydraDB dependency-learning demonstration observed the missing `order.details` prerequisite, applied it, and succeeded on a different order.
- The final nine-case local result reports completion: full context 8/9, flat lexical 4/9, dependency-expanded flat 4/9, semantic candidate-expanded 4/9, graph with the learned dependency excluded 6/9, and HydraDB graph 8/9. The graph succeeds on the line-item and product/seller cases where its learned dependency restores the required order details tool. These are authored deterministic cases and descriptive only, not statistical evidence.

## Deployment and data constraint

The Render Blueprint is Free and now declares `GROQ_API_KEY` as a non-synced secret plus the Groq model setting. The secret is not in the repository. Local `.env` does not configure Render; add the key through the Render service environment settings if real-agent mode should be enabled in the hosted demo. The free service has ephemeral local graph storage and reseeds on restart. The service should remain non-commercial while it distributes the Olist subset.

The Olist source is the [Brazilian E-Commerce Public Dataset by Olist](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce), licensed CC BY-NC-SA 4.0. The adapted database is attributed and separately licensed in [data/README.md](../data/README.md). Commercial use requires a suitable separate data license. The refund behavior is a ContextGraph simulation, not an Olist policy.

## Remaining limits

The authored nine-case benchmark is too small for significance claims. The semantic method is deterministic local skill matching plus declared dependency expansion, not vector/embedding retrieval. The data source is historical and limited to a 500-order subset. Hosted graph state is not durable on Render Free. The real Groq check establishes that one real tool call and response flow works; it does not establish broad model accuracy or free-tier availability guarantees.

## HydraDB constraints

The ignored `hydradb-upstream/` checkout describes HydraDB as a Rust graph engine over SlateDB. The app exercises supported OpenCypher reads, bounded client-side dependency traversal, parameterized `UNWIND` node merge, and batched relationship writes. It avoids assuming complete Neo4j/Cypher compatibility. Local filesystem persistence is for development; durable hosted graph storage requires an object-store-backed deployment.

## Final judge-readiness sprint (November 2026)

### Objective-level answer checks

The previous real-agent `MISS` label came from comparing model-called tool names against a keyword-derived required-tool list. It did not check whether the answer stated the requested facts correctly or whether returned tool data supported them. The updated result keeps retrieval coverage, execution trace, and factual answer evaluation separate. Deterministic checks compare order status, payment totals and methods, line items, seller IDs, and delivery timing against actual tool outputs. Item price plus freight is not treated as the payment total. Missing actual delivery time does not imply non-delivery. The evaluator reports `SUCCESS`, `PARTIAL`, `FAILURE`, `BLOCKED`, or `UNVERIFIED`, with per-objective evidence and reasons. Claims without deterministic verifiers remain unverified rather than being scored by the same model.

### Real Groq agent learning evidence

The real-agent learning endpoint was run against the local HydraDB and the `openai/gpt-oss-20b` Groq model. With `order.items → order.details` removed, the training run retrieved only `order.items`; Groq selected that tool, and the actual executor rejected it because `order.details` had not run. The endpoint validated this observed prerequisite against the declared tool schema, applied the HydraDB edge, and retested a different order with the corrected graph. The recorded held-out run returned a source-supported `SUCCESS`, called `order.details` then `order.items`, and verified the requested product/category fact. Rollback then restored the demonstration edge weight to `1.0`; a fresh graph route confirmed the edge remains in the baseline state. The response and HydraDB `LearningUpdate` retain the actual trace, evidence, held-out order, and mutation ID. This demonstrates one successful transfer case, not a general learning guarantee.

### Paired real-agent pilot

`POST /api/evaluate/agent` runs the same held-out request across full context, flat lexical retrieval, dependency-expanded flat retrieval, graph retrieval with the learned edge withheld, and graph retrieval after learning. Cases run sequentially with equal per-strategy request/token/tool-call budgets. Each completed case-strategy result is checkpointed in HydraDB and is skipped on resume. This uses the same model and evaluator for each strategy.

The first one-case free-tier pilot was attempted with a 20-request cap, then resumed with the 30-request endpoint ceiling after a provider rate limit. It recorded **18 provider attempts** across the original run and resume, and completed two of five strategies before Groq rate-limited the next strategy again. Both completed rows were `UNVERIFIED`: full context made three actual tool calls (including recovering from an initial dependency-order error), while flat lexical retrieval exposed `order.items` without its prerequisite and its call failed. This partial result is checkpointed; it does not support a graph win claim. Other strategy measurements are unavailable because the free-tier provider returned 429 responses within the configured retry window. Resume can continue from the saved checkpoint if provider quota permits.

### Updated verification

- Offline unit suite: 14 tests passed and one live integration test skipped because this test invocation had no local HydraDB configured (15 total). The live Groq integration had passed earlier against the local HydraDB instance.
- Python compile check and JavaScript syntax check passed.
- Live Groq integration and real learning/rollback were verified earlier in this sprint against local HydraDB. No provider key is committed or included in this document.
- Commit `297ee8a` was pushed to `main` and verified on Render. `/api/ready` and `/api/health` returned 200, the deployed OpenAPI includes `/api/evaluate/agent` and `/api/learning/agent-demo`, and a hosted fixture run verified status and payment total (`SUCCESS`, 2/2 objectives). `/api/agent/status` reports Groq available; the credential remains a server-side Render secret.

### Remaining limits

The Groq comparison is currently an incomplete, single-case pilot due to free-tier rate limits; it must not be presented as evidence of comparative superiority. The deterministic Olist objective checks cover only implemented task types, and the benchmark prompts are authored rather than statistically sampled. The app still uses local SQLite, and Render Free filesystem state is ephemeral.
