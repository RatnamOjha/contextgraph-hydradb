# ContextGraph SDK local quick start

This preview keeps developer catalogs in a separate local HydraDB store. It does not seed or reset the existing `.hydradb/` workspace, and it makes no Groq calls.

## 1. Start the isolated SDK backend

From the repository root:

```bash
./scripts/sdk-dev.sh
```

The first run builds HydraDB and creates `.hydradb-sdk/`. The API listens on `http://127.0.0.1:8001`; HydraDB uses separate ports from the regular demo. This workspace enables tool registration only for loopback development and does not seed the Olist catalog. Stop it with Ctrl-C.

## 2. Run the developer example

In another terminal, from the repository root:

```bash
.venv/bin/python examples/basic_retrieval.py
```

The example starts the read-only dummy MCP server, imports five tool definitions, declares `commerce.check_refund → commerce.get_order`, and retrieves context for a refund task. It prints the selected tools, JSON schemas, dependency edge, completion state, and the fact that no tool execution occurred.

The async version is:

```bash
.venv/bin/python examples/async_retrieval.py
```

The adapter example returns schema dictionaries for the developer's existing agent runtime:

```bash
.venv/bin/python examples/agent_adapter.py
```

After running the basic example at least once, run the labeled retrieval comparison:

```bash
.venv/bin/python examples/evaluate_catalog.py
```

The SDK distribution is not published to PyPI. `scripts/sdk-dev.sh` installs the checkout into `.venv` for local use. The backend requires a locally built HydraDB binary; first-time compilation needs Rust and the platform build prerequisites documented in [local development](DEPLOYMENT.md).

## Security and current scope

The MCP command is explicit local code execution. Only connect to a server you trust. The SDK discovers metadata and never calls the business tools. Developer catalog writes are disabled on the public Render service and are not protected by user authentication; the local catalog ID is not secure multitenancy. Retrieval uses deterministic lexical ranking and explicit prerequisite edges, not embeddings. See the [SDK architecture contract](SDK_ARCHITECTURE.md) for limitations.
