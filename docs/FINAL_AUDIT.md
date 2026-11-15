# Final engineering audit

## Delivered

- HydraDB remains the live graph for task/skill/tool relationships, transitive tool prerequisites, Olist order/customer/item/product/seller entity links, and execution evidence. SQLite answers minimized historical business-data queries.
- The deterministic fixture path runs against source rows and remains reproducible. A separate Groq agent path sends only graph-retrieved Olist tools, validates model arguments and dependencies, executes calls against SQLite, and produces an answer. A real Groq integration test completed successfully with one bounded case. Local development uses ignored `.env`; Render uses service-side environment variables. Neither contains a credential in Git.
- The order ID is parsed from each request. Both the relational record and matching HydraDB entity path must exist for graph mode; unknown IDs fail closed.
- The importer creates a deterministic 500-order subset and omits customer location, reviews, free text, and geolocation. The source archive remains local and gitignored; the curated SQLite subset is bundled with the app image.
- The deterministic held-out suite has nine separate cases, including unfamiliar phrasing and overlapping tool descriptions. It compares full context, flat lexical retrieval, dependency-expanded flat, semantic skill-candidate plus dependencies, graph retrieval with learning updates excluded, and HydraDB graph retrieval. Per-case expected tools, selected tools, entity match, and execution outcomes are returned. No model calls are made unless the optional real-agent subset is requested.
- The learning demonstration withholds an existing declared prerequisite through a request-scoped retrieval filter, records the observed failure, validates the prerequisite against tool metadata, removes the filter for a distinct-order retest, and exposes a baseline verification step. The dependency edge itself is unchanged.
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

The Render Blueprint is Free and declares `GROQ_API_KEY` as a non-synced secret plus the Groq model setting. The actual value is configured outside the repository. Local `.env` is separate and does not configure Render. The free service has ephemeral local graph storage and reseeds on restart. The service should remain non-commercial while it distributes the Olist subset.

The Olist source is the [Brazilian E-Commerce Public Dataset by Olist](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce), licensed CC BY-NC-SA 4.0. The adapted database is attributed and separately licensed in [data/README.md](../data/README.md). Commercial use requires a suitable separate data license. The refund behavior is a ContextGraph simulation, not an Olist policy.

## Remaining limits

The authored nine-case benchmark is too small for significance claims. The semantic method is deterministic local skill matching plus declared dependency expansion, not vector/embedding retrieval. The data source is historical and limited to a 500-order subset. Hosted graph state is not durable on Render Free. The real Groq check establishes that one real tool call and response flow works; it does not establish broad model accuracy or free-tier availability guarantees.

## HydraDB constraints

The ignored `hydradb-upstream/` checkout describes HydraDB as a Rust graph engine over SlateDB. The app exercises supported OpenCypher reads, bounded client-side dependency traversal, parameterized `UNWIND` node merge, and batched relationship writes. It avoids assuming complete Neo4j/Cypher compatibility. Local filesystem persistence is for development; durable hosted graph storage requires an object-store-backed deployment.

## Final judge-readiness sprint (November 2026)

### Objective-level answer checks

The previous real-agent `MISS` label came from comparing model-called tool names against a keyword-derived required-tool list. It did not check whether the answer stated the requested facts correctly or whether returned tool data supported them. The updated result keeps retrieval coverage, execution trace, and factual answer evaluation separate. Deterministic checks compare order status, payment totals and methods, line items, seller IDs, and delivery timing against actual tool outputs. Item price plus freight is not treated as the payment total. Missing actual delivery time does not imply non-delivery. The evaluator reports `SUCCESS`, `PARTIAL`, `FAILURE`, `BLOCKED`, or `UNVERIFIED`, with per-objective evidence and reasons. Claims without deterministic verifiers remain unverified rather than being scored by the same model.

### Real Groq agent learning evidence

A historical real-agent learning run against local HydraDB and `openai/gpt-oss-20b` recorded a source-supported held-out `SUCCESS` after Groq selected `order.items` without `order.details`, and runtime dependency validation rejected that call. That run predates this final implementation. During final verification, a direct `MATCH ... SET r.weight = 0` attempt on the existing `order.items -[DEPENDS_ON]-> order.details` edge returned HTTP 500 from the local HydraDB build; a read immediately afterward confirmed the edge remained present at weight `1.0`. The current implementation therefore uses the router's supported `exclude_dependencies` filter for that one retrieval call. A live route comparison confirmed `order.details` is present in the normal result and absent in the filtered result while the persisted edge remains at weight `1.0`. The local deterministic learning endpoint completed the training failure and distinct-order held-out success, then rollback returned `rolled_back` with the edge still at weight `1.0`; `evidence_store` correctly reported `local_sidecar` because this HydraDB writer rejected the LearningUpdate writes. The validated correction removes the request filter; rollback compares the edge's existence, weight, and properties with the stored pre-experiment snapshot and records the check without writing the edge. A `LearningUpdate` node stores the before/experiment/evidence/correction/rollback record when HydraDB accepts the mutation; otherwise the ignored atomic `data/learning-updates/` sidecar stores it and the API reports `evidence_store: local_sidecar`. Render Free storage is ephemeral. The logical edge identifier is `Tool/{source-id}-[DEPENDS_ON]->Tool/{target-id}`; the current run does not alter its sole `weight` property. The new retrieval-filter path was verified locally without another Groq request, so historical held-out success is not a result for this exact current code path.

### Paired real-agent pilot

`POST /api/evaluate/agent` runs the same held-out request across full context, flat lexical retrieval, dependency-expanded flat retrieval, graph retrieval with the learned edge withheld, and graph retrieval after learning. Cases run sequentially with equal per-strategy request/token/tool-call budgets. Each completed case-strategy result is written as an immutable HydraDB checkpoint revision when the graph runtime supports the write. This local runtime returns HTTP 500 for the required `UNWIND/MERGE/SET` mutation, so the runner preserves the latest snapshot in an ignored atomic local sidecar and reads the older HydraDB checkpoint as its base. This uses the same model and evaluator for each strategy.

The first one-case free-tier pilot used the `openai/gpt-oss-20b` model and the frozen `line_items` task for Olist order `03ef5dedbe7492bdae72eec50764c43f`. The original pilot used **18 provider attempts**. A final bounded resume used the remaining 12 attempts for dependency-expanded flat: 11 HTTP 200 responses and one HTTP 429. HydraDB returned HTTP 500 while saving the pair, so no model output, task result, tokens, tool calls, or latencies for that pair were persisted. The original HydraDB checkpoint remains intact with two completed rows. An ignored local snapshot adds the observed 12 attempts without inventing a task result. Total observed attempts are 30; known successful 2xx provider responses are 19 (8 from the two saved rows plus 11 in the resume), ten legacy attempts remain unclassified, and task success for dependency-expanded flat remains `NOT_RUN`. An accidental second bounded call occurred after the first returned a 503 persistence wrapper; no additional provider calls were made after the repeated run reached 429.

| Strategy | Run status | Task outcome | Actual tool calls | Input / output tokens | Retrieval / end-to-end latency | Provider accounting |
|---|---|---|---:|---:|---:|---|
| Full context | Complete | `UNVERIFIED` | 3 | 4,031 / 706 | 0.00 / 2,538.62 ms | 4 attempts; 4 successful responses |
| Flat lexical | Complete | `BLOCKED` after re-scoring saved evidence with the plural-dependency fix | 1 | 2,346 / 564 | 0.09 / 2,457.09 ms | 4 attempts; 4 successful responses |
| Flat with dependency expansion | Rate limited | `NOT_RUN` | — | — | — | 22 attempts; 11 HTTP 200 in the final resume; total successful-response count unknown |
| HydraDB graph with learning withheld | `NOT_RUN` | `NOT_RUN` | — | — | — | 0 attempts |
| HydraDB graph after learning | `NOT_RUN` | `NOT_RUN` | — | — | — | 0 attempts |

The full-context agent recovered from one initial dependency-order rejection and successfully called `order.details` and `order.items`, but its final answer did not state verifiable item facts. The lexical strategy called `order.items` without `order.details`; the saved execution output records the prerequisite rejection. Its corrected outcome is `BLOCKED`, not a task failure or success. The run is preserved and resumable. The real-Groq comparative pilot is incomplete due to provider free-tier limits. No comparative superiority claim is made.

### Updated verification

- The final-fixes sprint adds mocked 429/backoff, resume accounting, local checkpoint fallback, and graph state tests; current full-suite results are listed in the final report.
- Full offline suite: 28 tests run, 27 passed, one live Groq integration test skipped to avoid additional provider calls. Python compilation, JavaScript syntax, `git diff --check`, and Render Blueprint secret-name checks passed. A tracked-file scan found no Groq key-pattern matches.
- Local HydraDB read-only verification showed the dependency tool in the normal route and omitted it under the request-scoped filter while the stored edge remained present at weight `1.0`. Direct dependency-edge `SET` and checkpoint `UNWIND/MERGE/SET` calls returned HTTP 500 in this local runtime; no graph-edge write occurred. The fallback snapshot at `data/evaluation-checkpoints/` records the measured provider attempts without adding a third result row.
- Live Groq integration and real learning/rollback were verified earlier in this sprint against local HydraDB. No provider key is committed or included in this document.
- Commits `69ec35f`, `7df67d3`, and `1612b9e` were pushed to `main`. The post-push Render smoke returned 200 from `/api/health`, `/api/ready`, `/api/agent/status`, `/openapi.json`, and `/`; OpenAPI included the evaluation and learning endpoints, and the served UI contained the retrieval-filter wording. The hosted agent status reports configured=true, dataset available, model `openai/gpt-oss-20b`, and `provider_call_status=not_probed`. A deterministic full-context fixture request returned `task_success=true` with five tool calls. No hosted Groq or learning mutation was run. The credential remains a server-side Render secret.

### Remaining limits

The Groq comparison is currently an incomplete, single-case pilot due to free-tier rate limits; it must not be presented as evidence of comparative superiority. The deterministic Olist objective checks cover only implemented task types, and the benchmark prompts are authored rather than statistically sampled. `/api/agent/status` reports key configuration, not whether the model currently accepts calls. The app still uses local SQLite, and Render Free filesystem state is ephemeral.
