# ContextGraph

**More tools. Smaller agent context.** ContextGraph stores skills, tools, dependencies, entity links, and execution evidence in HydraDB, then retrieves a task-specific tool set for an agent. This hackathon demo uses historical Brazilian e-commerce data from Olist.

## Demo

[Open ContextGraph](https://contextgraph-demo.onrender.com). The Render Blueprint is configured for the Free plan. Free services sleep when idle and use ephemeral local storage, so the graph is reseeded after restart. The bundled SQLite demo subset is included in the image.

The dashboard has three views: a graph-routed order workflow with deterministic fixture and real Groq agent modes; a controlled dependency-recovery experiment; and a searchable tool registry. A separate sequential real-Groq pilot compares full context, flat lexical retrieval, flat retrieval with prerequisite expansion, and HydraDB graph retrieval. Each observed result gets a stable ID, a local recovery journal entry, and a HydraDB write/readback before the case is marked complete.

### Execution modes

`POST /api/run` accepts `execution_mode: "fixture" | "agent"` and `method: "graph" | "flat" | "expanded_flat" | "full"`. Fixture mode reads actual Olist rows through deterministic tools. Agent mode sends only the selected method's read-only tool definitions to Groq, accepts model-chosen JSON arguments, validates dependencies and entity IDs, executes the calls against the local data store, and returns the model's answer. Provider errors do not fall back to fixture mode. The result separates retrieval, execution, and answer evaluation; outcomes are `SUCCESS`, `PARTIAL`, `FAILURE`, `BLOCKED`, or `UNVERIFIED`, with deterministic objective checks where the data supports them. Configure `GROQ_API_KEY` and optionally `GROQ_MODEL` (default `openai/gpt-oss-20b`) on the API server. For local development, keep the key in ignored `.env`; on Render, set it as a service environment secret. `GET /api/agent/status` reports whether the key is configured and the dataset is present; it does not probe Groq, so it is not a provider-health check. The deterministic evaluation makes no model calls. `POST /api/evaluate/agent` runs a sequential paired pilot (up to three cases) with request, token, and tool-call limits. A result is complete only after exact HydraDB readback. On write failure, the result stays pending in an ephemeral local journal and resume replays it before any new provider call. On Render Free, the HydraDB store shares the ephemeral service filesystem, so readback does not guarantee survival across instance replacement. No external durable store is configured.

The entity retriever extracts a 32-character Olist order ID from each request and requires both a matching SQLite row and its HydraDB entity path. Unknown IDs fail closed. Refund eligibility is a clearly labeled simulated demo policy, not an Olist policy.

The current real-Groq paired pilot is incomplete due to free-tier provider limits. Its preserved results and explicit `NOT_RUN` statuses are documented in [the real-agent benchmark report](docs/REAL_AGENT_BENCHMARK.md); no comparative superiority claim is made.

## Local setup

Python 3.10+ and the dependencies in `requirements.txt` are required. A local HydraDB server is also required; the project documents its checked-out upstream source and supported query path in [docs/HYDRADB_PHASE0.md](docs/HYDRADB_PHASE0.md). Set `HYDRADB_URL` as needed. Copy `.env.example` to `.env`, then set `GROQ_API_KEY` locally if you want real-agent mode. Keep the key out of source control and chat.

Import the official Olist archive into the documented 500-order subset:

```bash
python scripts/import_olist.py --archive data/raw/olist.zip --output data/olist_demo.sqlite --orders 500
```

Start the local stack with `./scripts/dev.sh`, then open [http://127.0.0.1:8000](http://127.0.0.1:8000). Unit tests run with:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

The real Groq integration test runs only when `GROQ_API_KEY` and a local HydraDB instance are available. The deterministic evaluation is available at `POST /api/evaluate`; `/api/learning/demo` runs controlled deterministic dependency recovery; `/api/learning/agent-demo` runs controlled recovery with real Groq failure evidence and a held-out retest; and `POST /api/learning/rollback/{update_id}` verifies the recorded pre-experiment edge state. Both learning demos keep the existing `DEPENDS_ON` relationship unchanged and use a request-scoped retrieval filter. They are not graph learning. The isolated named-graph write scope required for a separate corrected-graph experiment is not available in the current product runtime, so no graph mutation or learning improvement is claimed. Learning evidence is read back from HydraDB when available; the ignored `data/learning-updates/` sidecar is ephemeral. The Render Free graph store is ephemeral across instance replacement as well. See [HydraDB persistence and learning validation](docs/hydradb-persistence.md) for query constraints, failure diagnosis, deterministic and isolated graph-learning baselines. Run the local isolated experiment with `.venv/bin/python scripts/graph_learning_experiment.py --heldout 3`; it starts a separate temporary HydraDB process and makes no Groq calls.

## Data license and attribution

The bundled SQLite database is a 500-order subset of the [Brazilian E-Commerce Public Dataset by Olist](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce), attributed to Olist and licensed **CC BY-NC-SA 4.0**. It is distributed separately from the MIT application code. The dataset license prohibits commercial use and requires attribution and ShareAlike for adaptations; do not use the Olist-backed demo in a paid or other commercial service without a suitable separate license. See [data/README.md](data/README.md) and the [license deed](https://creativecommons.org/licenses/by-nc-sa/4.0/). The importer omits customer location, reviews, free text, and geolocation.

## Limitations

The authored fixture cases and the real-Groq pilot are descriptive, small samples, not statistically powered benchmarks. Real-agent results depend on Groq's model behavior and free-tier availability. The pilot can pause on provider or persistence errors; confirmed results are not repeated on resume. The semantic fixture baseline uses local skill candidate matching and dependency expansion, not a learned embedding model. The refund rule is simulated. Render Free storage is ephemeral. The app code is MIT licensed; HydraDB and Olist retain their separate upstream licenses.

## License

ContextGraph application code: MIT. Olist subset: CC BY-NC-SA 4.0. HydraDB is a separate dependency; see its upstream repository for its license.
