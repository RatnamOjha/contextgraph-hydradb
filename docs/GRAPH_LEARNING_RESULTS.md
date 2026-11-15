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
- The prerequisite was validated from declared tool metadata. This does not demonstrate autonomous dependency discovery.
- The harness uses a temporary HydraDB process and store. It proves the mutation, readback, held-out retest, and rollback path locally; it is not a mutation against the shared Render graph.
- The product API’s existing learning demo remains controlled dependency recovery: it withholds an existing edge through a request-scoped retrieval filter and does not mutate the graph.
- The older populated local HydraDB store’s generic HTTP 500 remains unexplained. This fresh-store experiment does not repair or diagnose that store.
- The harness prints the report to stdout; preserve the JSON output separately if you need a recording artifact.
