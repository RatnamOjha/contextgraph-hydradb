# Synthetic Company: Full Catalog vs ContextGraph

## Result

A fictional support company with four mock MCP catalogs provides a useful first product benchmark. Across the same three deterministic workflows, ContextGraph graph retrieval preserved 3/3 task completions while reducing estimated tool-definition context by 55.6% and executed fixture calls by 33.4% against the full-catalog baseline.

These are local synthetic measurements. They are not a claim about production agents or real MCP services.

## Company and setup

The demo company is represented by the existing Support Desk, Commerce, Billing, and Shipping tool catalogs. The company-only catalog contains 18 synthetic tool definitions, five skills, and three workflows. Tool results and entities are deterministic fixtures; no external company system is contacted.

The graph connects each workflow to its relevant skills, skills to tools, tools to MCP servers, tools to prerequisites, and known entity paths. Before this experiment, all workflows had equal edges to every skill, and routing selected the alphabetically first skill. The seed now records only each workflow's actual skills, and graph routing follows all of those skill relationships.

## Exact execution command

Start the isolated in-memory company graph and API:

```bash
CONTEXTGRAPH_CATALOG_SCOPE=company \
CONTEXTGRAPH_STATE_DIR=/tmp/contextgraph-company-only \
CONTEXTGRAPH_API_PORT=18004 \
HYDRA_HTTP_PORT=18448 \
HYDRA_BOLT_PORT=17692 \
HYDRA_ADMIN_PORT=19096 \
CONTEXTGRAPH_CATALOG_SIZE=20 \
./scripts/dev.sh
```

In another terminal, run the paired benchmark:

```bash
.venv/bin/python scripts/benchmark_company.py --base-url http://127.0.0.1:18004
```

The runner compares `full`, lexical `flat`, dependency-expanded flat, and `graph` retrieval on the same refund, billing, and delivery tasks. It makes no LLM/provider calls. Raw per-task results are in [`results/company_contextgraph_benchmark.json`](../results/company_contextgraph_benchmark.json).

## Full catalog vs graph retrieval

| Workflow | Completion | Tool context estimate | Irrelevant tools exposed | Executed fixture calls |
|---|---:|---:|---:|---:|
| Refund — full catalog | Pass | 1,661 | 13 | 5 |
| Refund — ContextGraph | Pass | 767 | 5 | 3 |
| Billing — full catalog | Pass | 1,661 | 14 | 4 |
| Billing — ContextGraph | Pass | 545 | 3 | 3 |
| Delivery — full catalog | Pass | 1,661 | 12 | 6 |
| Delivery — ContextGraph | Pass | 901 | 6 | 4 |
| **Mean — full catalog** | **3/3** | **1,661** | **13.0** | **5.00** |
| **Mean — ContextGraph** | **3/3** | **737.7** | **4.7** | **3.33** |

ContextGraph exposed 8.3 fewer irrelevant tool definitions per task on average. Fixture execution used the graph's known ticket/customer/order path to reuse entity context, which accounts for much of the lower call count. Required-tool recall was 1.0 for both full and graph methods.

## Flat retrieval controls

| Method | Completion | Mean context estimate | Mean irrelevant tools exposed | Mean fixture calls | Required-tool recall |
|---|---:|---:|---:|---:|---:|
| Full catalog | 3/3 | 1,661 | 13.0 | 5.00 | 1.000 |
| Lexical flat | 0/3 | 743 | 4.7 | 0.67 | 0.672 |
| Dependency-expanded flat | 1/3 | 713 | 3.3 | 4.33 | 0.861 |
| ContextGraph | 3/3 | 738 | 4.7 | 3.33 | 1.000 |

The flat methods expose fewer definitions but miss required tools often enough to fail these fixture tasks. The graph method matches the full baseline's completion while using less estimated tool context.

## What “unnecessary calls” means here

The deterministic fixture runner executes only declared required tools. It recorded **zero unnecessary executed calls** for both full and graph methods. This benchmark therefore does **not** measure whether an LLM would call fewer unnecessary tools after seeing a smaller catalog. It measures:

- Tool definitions made available to the agent, including irrelevant definitions.
- Deterministic fixture calls needed to complete each task.
- Required-tool recall and fixture task completion.

A real-agent paired evaluation with the same model, prompts, data, and budgets is needed to measure actual LLM calls and billed tokens. This report intentionally makes no such claim and made no Groq calls.

## Token and evaluation limits

The token figure follows the app's current estimate: serialized selected tool definitions' JSON character count divided by four. It excludes system and user prompts, tool results, generated answers, tokenizer differences, and provider overhead. Treat it as a relative tool-context estimate, not billed token expenditure.

There are only three fixed workflows, all running deterministic fixtures. The synthetic company is a product demo scaffold, not a live MCP company or an independent generalization benchmark. The result establishes a reproducible comparison for this scope; it does not establish model quality or production savings.
