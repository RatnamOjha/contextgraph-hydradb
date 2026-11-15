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
