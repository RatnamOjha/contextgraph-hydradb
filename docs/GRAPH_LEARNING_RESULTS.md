# Graph learning experiment results

## Loom summary

**A prerequisite edge was missing from a fresh isolated HydraDB graph.** The Olist workflow failed on three held-out orders before the edge existed. After validating and persisting `order.items -[DEPENDS_ON]-> order.details`, all three held-out tasks passed. The edge was then deleted and HydraDB readback confirmed the original edge-free graph state.

This was deterministic execution over Olist data. **No Groq calls were made.** The dependency was checked against declared tool metadata; it was not autonomously discovered.

## Reproduce

From the repository root, run:

```bash
.venv/bin/python scripts/graph_learning_experiment.py --heldout 3
```

The harness starts a separate HydraDB process with a new temporary store, runs the experiment, prints a JSON report, and removes the temporary store when it exits. It does not connect to the shared Render graph.

## Tasks and split

- Training order: `54282e97f61c23b78330c15b154c867d`
- Training task: “List the item categories for reference `<order_id>`.”
- Held-out orders: `35a972d7f8436f405b56e36add1a7140`, `03ef5dedbe7492bdae72eec50764c43f`, `168626408cb32af0ffaf76711caae1dc`
- Held-out task: “Which goods categories appear in historical order `<order_id>`?”
- Training and held-out order IDs are disjoint.

## Results

| Method | Verified held-out completion | Dependency failures | Tool calls | Mean context estimate |
|---|---:|---:|---:|---:|
| Initial graph, dependency missing | 0/3 | 3 | 0 | 90 tokens |
| Corrected graph, edge persisted | 3/3 | 0 | 6 | 182 tokens |
| Flat lexical retrieval | 3/3 | 0 | 6 | 356 tokens |
| Flat retrieval with dependency expansion | 3/3 | 0 | 6 | 356 tokens |

One correction was validated. There were zero regressions. Completion is scored by the deterministic objective evaluator against the Olist tool outputs. Context is an estimate derived from serialized tool definitions.

## Regression controls

These independent controls are run against the same initial edge-free and corrected edge snapshots. They are reported separately from the three dependency-recovery held-out tasks. The workflow's required tool set supplies the retrieval roots, and dependency closure is computed from each snapshot.

| Control | Snapshot | Selected tools | Executed tools | Verified outcome | Dependency failures | Context estimate |
|---|---|---|---|---:|---:|---:|
| Details-only: “What is the status of order …?” | Before | `order.details` | `order.details` | 1/1 `SUCCESS` | 0 | 92 tokens |
| Details-only | After | `order.details` | `order.details` | 1/1 `SUCCESS` | 0 | 92 tokens |
| Payments: “Summarize payment methods, values, and installment counts …” | Before | `order.details`, `order.payments` | `order.details`, `order.payments` | 3/3 `SUCCESS` | 0 | 177 tokens |
| Payments | After | `order.details`, `order.payments` | `order.details`, `order.payments` | 3/3 `SUCCESS` | 0 | 177 tokens |

The corrected snapshot did not add `order.items` to either control. Completion, selected tools, and estimated context were unchanged in both; context overhead delta was 0 tokens. The payments workflow is a second unrelated negative control: it exercises payment method, value, and installment objectives while remaining independent of the `order.items → order.details` retrieval edge.

## Paired same-order dependency-direction controls

The paired run uses each of the same three held-out orders for both a status-only task and a product-category task. It is a separate regression experiment and does not change the original benchmark or count status tasks as dependency-recovery successes.

| Task | Before correction | After correction | Verified completion | Context estimate |
|---|---|---|---:|---:|
| Status only, same 3 orders | Selected/executed `order.details`; `order.items` absent | Selected/executed `order.details`; `order.items` absent | 3/3 → 3/3 | 92 → 92 tokens |
| Product categories, same 3 orders | Selected `order.items`; execution blocked by missing `order.details` | Selected `order.details` + `order.items`; executed `order.details` then `order.items` | 0/3 → 3/3 | 90 → 182 tokens |

Each status answer remained byte-for-byte identical for its order and verified correctly before and after. Each category task reported the missing `order.items → order.details` prerequisite before correction, then returned verified Olist category evidence after correction. The 92-token category context increase is the prerequisite tool definition required to complete the task; no unrelated tool was added. Across all six paired task comparisons, no status tool-selection, execution, answer, or context regression was detected.

The harness evaluates both edge snapshots in a fresh temporary HydraDB store. It keeps the original `order.items -[DEPENDS_ON]-> order.details` relationship direction; the executor runs the prerequisite `order.details` before `order.items`. The correction is deleted after the retest and the empty initial edge snapshot is confirmed by readback.

Held-out order IDs and per-order outcomes:

| Same order | Status before/after | Category before → after | Category context before → after |
|---|---|---|---:|
| `35a972d7f8436f405b56e36add1a7140` | `SUCCESS` / `SUCCESS` | `UNVERIFIED` → `SUCCESS` | 90 → 182 tokens |
| `03ef5dedbe7492bdae72eec50764c43f` | `SUCCESS` / `SUCCESS` | `UNVERIFIED` → `SUCCESS` | 90 → 182 tokens |
| `168626408cb32af0ffaf76711caae1dc` | `SUCCESS` / `SUCCESS` | `UNVERIFIED` → `SUCCESS` | 90 → 182 tokens |

## HydraDB mutation evidence

The initial graph held the `order.items` and `order.details` tool nodes, but no `DEPENDS_ON` relationship. The relationship query returned no rows:

```cypher
MATCH (a:Tool {id: $src})-[r:DEPENDS_ON]->(b:Tool {id: $dst})
RETURN a.key AS tool, b.key AS dependency, r.weight AS weight
```

The experiment confirmed that `order.details` appears in `order.items`’ declared `Tool.dependencies`. It then created the relationship using the supported batch edge operation:

```cypher
UNWIND $rows AS row
CREATE (a {id: row.src})-[:DEPENDS_ON]->(b {id: row.dst})
```

It set the relationship weight with `MATCH ... SET`, then ran the same relationship query. Readback returned:

```text
order.items | order.details | 1.0
```

The held-out retrieval used the edge read from HydraDB to select both tools; it did not apply a request-scoped exclusion filter. The corrected workflow executed `order.details` and `order.items` against each held-out Olist order, producing six tool calls total and three verified completions.

For rollback, the experiment deleted the relationship:

```cypher
MATCH (a:Tool {id: $src})-[r:DEPENDS_ON]->(b:Tool {id: $dst})
DELETE r
```

The same readback query returned no rows, matching the initial edge-free snapshot. The isolated `LearningUpdate` evidence record was also read back after writes; its final state was `rolled_back` (record ID `644578929030145457`).

## Limitations

- This is a small, three-order deterministic experiment, not a Groq or statistical benchmark.
- Regression controls use deterministic workflow-required roots and graph-edge closure, not a production natural-language retriever or a broad workload sample. Their unchanged tool/context counts establish this edge's direction-specific non-interference for these controls only.
- The paired tasks are deterministic controls over the same three Olist orders. The category root is deliberately `order.items` so the prerequisite edge's direction can be isolated; this is not a general-purpose task-ranking evaluation.
- The prerequisite was validated from declared tool metadata. This does not demonstrate autonomous dependency discovery.
- The harness uses a temporary HydraDB process and store. It proves the mutation, readback, held-out retest, and rollback path locally; it is not a mutation against the shared Render graph.
- The product API’s existing learning demo remains controlled dependency recovery: it withholds an existing edge through a request-scoped retrieval filter and does not mutate the graph.
- The older populated local HydraDB store’s generic HTTP 500 remains unexplained. This fresh-store experiment does not repair or diagnose that store.
- The harness prints the report to stdout; preserve the JSON output separately if you need a recording artifact.
