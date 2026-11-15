# HydraDB persistence and learning validation

## Runtime and write diagnosis

The local runtime inspected for this sprint reports `graph-node 0.1.0+916637477f3f`, Cypher engine `legacy`, `experimental_compiled=false`, `datafusion_compiled=false`, and mutation atomicity `legacy_chunked`. The build uses the `server-runtime` feature. This is not complete Neo4j/OpenCypher compatibility.

The exact `LearningUpdate` creation query failed on a fresh isolated store with HTTP 400:

```text
{"error":{"code":"invalid_request","message":"OpenCypher query is not supported yet: UNWIND vertex SET values must read fields from the row map"}}
```

The sanitized query shape was:

```cypher
UNWIND $rows AS row
MERGE (u {id: row.id})
SET u:LearningUpdate, u.tool=row.tool, u.relationship_type='DEPENDS_ON',
    u.state='experimenting', u.evidence='{}'
```

The cause was literal property assignments inside `UNWIND ... SET` (`state='experimenting'`, `evidence='{}'`, and `relationship_type='DEPENDS_ON'`). The legacy parser requires every assigned value to read from the current row map. Moving these values into row fields made the write return HTTP 200 and allowed exact readback.

A parameter value of `null` is not supported. A minimal property update with null returned HTTP 400:

```text
{"error":{"code":"invalid_parameter","message":"parameter $value must contain booleans, signed or unsigned integers, finite floats, strings, lists, or string-keyed maps"}}
```

Assigning a list-valued row property to a node field also returned HTTP 400 (`{"error":{"code":"invalid_request","message":"OpenCypher query is not supported yet: UNWIND row 0 field dependencies must be scalar"}}`). The isolated graph harness now retains minimal probes for literal `UNWIND SET` and `null` parameters so these engine constraints stay visible in repeatable verification.

Learning updates now omit absent properties from the `SET` clause. Relationship creation with `UNWIND ... CREATE`, node `UNWIND ... MERGE ... SET`, node property updates with `MATCH ... SET`, and relationship property updates with `MATCH ... SET` were exercised against the isolated runtime; all successful writes were read back. The accepted checkpoint and result shapes use row-map assignments throughout.

The pre-existing populated local store returned HTTP 500 for these minimal supported write shapes (using unique probe IDs and scalar parameters):

```cypher
UNWIND $rows AS row
MERGE (n {id: row.id})
SET n:PersistenceProbe, n.label=row.label

UNWIND $rows AS row
CREATE (a {id: row.src})-[:PersistenceProbeRel]->(b {id: row.dst})

UNWIND $rows AS row
MERGE (c {id: row.id})
SET c:EvaluationCheckpoint, c.run_id=row.run_id, c.revision=row.revision,
    c.state=row.state, c.updated_at=row.updated_at, c.payload=row.payload
```

Each returned the same generic sanitized response:

```text
{"error":{"code":"internal","message":"internal query execution error"}}
```

Its admin endpoint still reports `legacy`; `/metrics` recorded 21 storage-class query failures. Reads continued to work. The same query shapes succeeded on a fresh isolated store, so the 500 is store-state-specific, not explained by unsupported syntax. The running node's stdout was discarded and its logs contained no internal cause. Root cause of that particular 500 remains unconfirmed; do not treat the fresh-store result as repair of the old store.

## Evaluation persistence

Each observed case/strategy result is first written to an atomic local recovery journal, then upserted under a stable `EvaluationResult` ID derived from `run_id|case|strategy`. The complete result includes tool arguments and outputs, objective outcomes, usage/token accounting, latency, and provider attempts. The writer reads back and compares the serialized result before setting `persistence_status=hydradb_confirmed` or reporting the case complete.

If HydraDB write/readback fails, the run returns `paused_persistence_error`; the observed result remains in the journal and is not considered safely checkpointed. Resume queries graph-backed `EvaluationResult` nodes first, then replays journal-only records before any new Groq execution. This rebuilds a run if local checkpoint and journal files disappear while the HydraDB store remains available. On the current Render Free service, HydraDB and the app share the service filesystem; that store is also ephemeral across instance replacement. Therefore a HydraDB readback confirms a graph write, not external durability across a Render restart or redeploy. Stable IDs and `MERGE` make replay idempotent. Aggregate `EvaluationCheckpoint` revisions also use stable version IDs and verified readback. Local checkpoint and journal files are ephemeral on Render Free and are explicitly reported as `local_fallback_ephemeral` when the graph write fails. There is no external durable store configured; this work does not claim cross-instance durability.

A simulated 500 after a successful mocked provider response was tested: resume persisted the already observed result and made no second provider request for that case/strategy.

## Learning evidence and claims

`LearningUpdate` records contain the original task, training and held-out order IDs, an edge snapshot hash (`graph_version`), retrieved-context snapshot, actual failure evidence, proposed relationship, metadata validation, correction status, held-out retest, and rollback information. Create, final update, and rollback markers are read back from HydraDB. A local JSON sidecar remains a recovery convenience only and is ephemeral on Render Free.

The existing API demo is controlled dependency recovery: it withholds an existing `DEPENDS_ON` edge with a request-scoped retrieval filter and then removes the filter. It does not mutate the graph. A separate local-only harness, `scripts/graph_learning_experiment.py --heldout 3`, starts a fresh HydraDB process with a temporary store, creates a tool graph genuinely missing the edge, runs the deterministic Olist tool workflow, validates the proposed edge against declared `Tool.dependencies`, writes and reads back the relationship, runs held-out tasks using the corrected graph, deletes the relationship, and verifies the original graph snapshot. The product runtime's auth principal denied writes to a new named graph with HTTP 403 `permission_denied`, so this path runs in its own isolated HydraDB process and is not exposed as a shared Render operation. The prerequisite was validated from metadata; it was not autonomously discovered. The real-agent path remains a separate Groq workflow.

The latest isolated run used training order `54282e97f61c23b78330c15b154c867d` and three distinct held-out orders: `35a972d7f8436f405b56e36add1a7140`, `03ef5dedbe7492bdae72eec50764c43f`, and `168626408cb32af0ffaf76711caae1dc`.

| Method | Held-out verified completion | Dependency failures | Tool calls | Mean context estimate |
|---|---:|---:|---:|---:|
| Initial graph, missing edge | 0/3 | 3 | 0 | 90 tokens |
| Corrected graph, persisted edge | 3/3 | 0 | 6 | 182 tokens |
| Flat lexical retrieval | 3/3 | 0 | 6 | 356 tokens |
| Flat with dependency expansion | 3/3 | 0 | 6 | 356 tokens |

One correction was validated, with zero regressions. The edge was deleted and readback confirmed the initial graph snapshot. This is deterministic Olist execution, not a Groq result. The harness and its test use temporary stores and make zero model calls.

## Deterministic Olist baseline

The frozen suite reserves two Olist orders for training and evaluates nine distinct held-out order IDs. It makes zero model calls. The most recent local run produced:

| Method | Full-task completion | Required-tool recall | Mean context tokens | Mean tool calls |
|---|---:|---:|---:|---:|
| Full context | 88.9% | 100.0% | 936.0 | 2.33 |
| Flat lexical | 44.4% | 75.0% | 220.6 | 0.89 |
| Flat dependency-expanded | 44.4% | 75.0% | 220.6 | 0.89 |
| Semantic dependency-expanded | 44.4% | 75.0% | 220.6 | 0.89 |
| Current HydraDB graph | 88.9% | 96.3% | 272.1 | 2.33 |
| Controlled recovery filter | 66.7% | 84.3% | 241.1 | 1.44 |

These are descriptive deterministic tool-workflow results, not Groq model results. The controlled recovery filter is not a corrected graph. A paired initial-graph versus corrected-graph experiment remains unavailable until a separate graph scope can be authorized and tested end to end.
