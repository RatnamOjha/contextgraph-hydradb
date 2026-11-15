# ContextGraph

**More tools. Smaller agent context.** ContextGraph stores skills, tools, dependencies, entity links, and execution evidence in HydraDB, then retrieves a task-specific tool set for an agent. This hackathon demo uses historical Brazilian e-commerce data from Olist.

## Demo

[Open ContextGraph](https://contextgraph-demo.onrender.com). The Render Blueprint is configured for the Free plan. Free services sleep when idle and use ephemeral local storage, so the graph is reseeded after restart. The bundled SQLite demo subset is included in the image.

The dashboard has three views: a graph-routed order workflow with deterministic fixture and real Groq agent modes; a dependency failure, evidence validation, graph update, held-out retest, and rollback; and a searchable tool registry. The held-out comparison reports graph, lexical, dependency-expanded, semantic candidate, graph-learning-disabled, and full-context methods case by case.

### Execution modes

`POST /api/run` accepts `execution_mode: "fixture" | "agent"` and `method: "graph" | "flat" | "expanded_flat" | "full"`. Fixture mode reads actual Olist rows through deterministic tools. Agent mode sends the graph-retrieved read-only tool definitions to Groq, accepts model-chosen JSON arguments, validates dependencies and entity IDs, executes the calls against the local data store, and returns the model's answer. Provider errors do not fall back to fixture mode. Configure `GROQ_API_KEY` and optionally `GROQ_MODEL` (default `openai/gpt-oss-20b`) on the API server. `GET /api/agent/status` reports availability without exposing credentials. The optional real-model evaluation is limited to three cases; the default nine-case evaluation makes no model calls.

The entity retriever extracts a 32-character Olist order ID from each request and requires both a matching SQLite row and its HydraDB entity path. Unknown IDs fail closed. Refund eligibility is a clearly labeled simulated demo policy, not an Olist policy.

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

The real Groq integration test runs only when `GROQ_API_KEY` is configured. The deterministic evaluation is available at `POST /api/evaluate`; `/api/learning/demo` runs the dependency evidence demonstration, and `POST /api/learning/rollback/{update_id}` rolls back its update.

## Data license and attribution

The bundled SQLite database is a 500-order subset of the [Brazilian E-Commerce Public Dataset by Olist](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce), attributed to Olist and licensed **CC BY-NC-SA 4.0**. It is distributed separately from the MIT application code. The dataset license prohibits commercial use and requires attribution and ShareAlike for adaptations; do not use the Olist-backed demo in a paid or other commercial service without a suitable separate license. See [data/README.md](data/README.md) and the [license deed](https://creativecommons.org/licenses/by-nc-sa/4.0/). The importer omits customer location, reviews, free text, and geolocation.

## Limitations

The nine held-out prompts are authored, descriptive cases rather than a statistically powered benchmark. Execution outcomes are deterministic over source rows; they do not establish LLM reasoning quality. The semantic baseline uses local skill candidate matching and dependency expansion, not a learned embedding model. The refund rule is simulated. Render Free storage is ephemeral. The app code is MIT licensed; HydraDB and Olist retain their separate upstream licenses.

## License

ContextGraph application code: MIT. Olist subset: CC BY-NC-SA 4.0. HydraDB is a separate dependency; see its upstream repository for its license.
