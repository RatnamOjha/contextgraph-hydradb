# Real Groq paired retrieval pilot

**Current finding:** The real-Groq comparative pilot is incomplete due to provider free-tier limits. No comparative superiority claim is made.

## Frozen comparison

The runner uses `openai/gpt-oss-20b`, a frozen Olist order and task, the same local Olist tool runtime, the deterministic objective evaluator, and equal per-strategy request/token/tool-call budgets. The five strategies are full context, flat lexical, dependency-expanded flat, HydraDB graph with the dependency withheld by a request-scoped retrieval filter, and HydraDB graph after the filter is removed. Runs are sequential, checkpointed after each completed case-strategy pair, and resume skips completed pairs.

## Preserved checkpoint and bounded resume

The original HydraDB checkpoint `2e022c59a471ff170fef5c6f` remains intact with its two completed rows and 18 historic attempts. A final bounded resume consumed the remaining 12 attempts for `flat_dependency_expanded`: 11 returned HTTP 200 and one returned HTTP 429. Checkpoint writes then failed with HTTP 500 from the local HydraDB runtime, so the model output, task outcome, tokens, tool calls, and latency for that strategy were not persisted. The two runs did not produce a valid task-level result and the strategy remains `NOT_RUN` for outcome scoring. The HTTP 200 responses are provider responses, not proof of task success.

An operator retry was mistakenly issued after the first API call returned a 503 persistence wrapper. Its logs show the second bounded group of six requests. No further provider calls were made. The accounting is honest: 30 attempts are observed in total; 19 successful 2xx responses are known (the eight from completed historical rows plus eleven from the resume); ten historic attempts remain unclassified; one additional 429 is directly observed. The total successful-response count for the dependency-expanded strategy remains unknown because its ten historic attempts lack response-level accounting.

| Strategy | Completion | Task outcome | Actual tool calls | Input / output tokens | Retrieval / end-to-end latency | Provider attempts / known successful responses |
|---|---|---|---:|---:|---:|---:|
| Full context | `COMPLETE` | `UNVERIFIED` | 3 | 4,031 / 706 | 0.00 / 2,538.62 ms | 4 / 4 |
| Flat lexical | `COMPLETE` | `BLOCKED` | 1 | 2,346 / 564 | 0.09 / 2,457.09 ms | 4 / 4 |
| Dependency-expanded flat | `RATE_LIMITED` | `NOT_RUN` | Not captured | Not captured | Not captured | 22 / unknown total; 11 HTTP 200 observed in the final resume |
| HydraDB graph, dependency withheld | `NOT_RUN` | `NOT_RUN` | — | — | — | 0 / 0 |
| HydraDB graph after correction | `NOT_RUN` | `NOT_RUN` | — | — | — | 0 / 0 |

`UNVERIFIED` means the full-context answer did not state the requested line-item facts even though item evidence was returned. `BLOCKED` means flat lexical selected `order.items` without `order.details`; runtime rejected that call because the prerequisite had not executed. `NOT_RUN` is not a task failure. The flat lexical outcome was re-scored from its saved evidence with the plural-dependency classifier fix; no prompt or criteria changed.

The local HydraDB checkpoint still has the original 18-attempt snapshot. A local ignored sidecar at `data/evaluation-checkpoints/` stores revision 1 with the observed total 30 attempts and leaves the two original result rows intact. This fallback is atomic and supports resume within an instance, but it cannot survive Render Free restarts. The local HydraDB build rejects checkpoint `UNWIND/MERGE/SET` writes and LearningUpdate writes (HTTP 400 or 500, depending on the query path); this is the persistence defect that blocked output capture. The service reports the failure and saves a local sidecar instead of silently losing the in-memory result.

The real-Groq comparative pilot is incomplete due to provider free-tier limits. No comparative superiority claim is made. No further live Groq calls should be made for this pilot under the current free-tier state.

## Rate-limit and accounting controls

The runner honors numeric or HTTP-date `Retry-After`, uses Groq reset headers when available, and otherwise uses bounded exponential backoff. It distinguishes request-per-minute, token-per-minute, and daily-quota errors when provider fields identify them. Daily exhaustion is not retried. Every retry consumes the same finite per-strategy provider-attempt budget. It tracks attempted requests, received responses, successful 2xx responses, 429 responses, tokens, and tool calls separately. Provider errors and partial accounting are visible in the UI; legacy unknowns stay explicitly unknown.

The runner uses one bounded sequential request budget, and attempts include retries. It honors numeric and HTTP-date `Retry-After`, provider reset headers, and bounded backoff. Daily quota errors are not retried. The attempt/response accounting is explicit; legacy and unpersisted task results remain unknown. The current checkpoint writer falls back to an ignored atomic local snapshot when this HydraDB build rejects its write query.

## Learning-path implementation note

The dependency relationship remains present at weight `1.0`. The final code withholds `order.items -[DEPENDS_ON]-> order.details` through the router's `exclude_dependencies` argument for the training retrieval, then omits that filter for the held-out route. A local read-only comparison confirmed the dependency tool disappears only in the filtered result while the stored edge stays unchanged. The deterministic fixture learning endpoint was also exercised end to end: the filtered training route failed on the declared prerequisite, the distinct held-out route succeeded, and rollback verified the original weight. Its evidence record used the local fallback because HydraDB rejected the writer query. A direct edge-weight write against this local HydraDB build returned HTTP 500, so the final implementation avoids that unsupported operation. Rollback records verification of the unchanged pre-experiment edge snapshot; the `LearningUpdate` evidence node is the only graph mutation. The earlier Groq learning success is historical and predates this final retrieval-filter implementation.
