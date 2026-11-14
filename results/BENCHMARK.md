# Benchmark results

Generated with `scripts/benchmark.py` on the local one-node HydraDB stack. This is a fixed-fixture engineering comparison, not a general claim about agent quality.

## Method

- Catalog sizes: 20, 100, and 500 registered MCP-like tools.
- Three fixed support tasks; five repetitions per task and strategy (15 requests per cell).
- Four strategies share the same tool catalog: full context; in-memory flat keyword retrieval; HydraDB label scan plus the same lexical ranker (graph ablation); and HydraDB graph traversal.
- Graph route retrieves weighted task-to-skill paths, the relevant tool neighborhood and dependencies, and known entities. Tool fixtures execute deterministically; no LLM or network MCP server is used.
- Success, recall, and unnecessary-tool counts are measured against fixture-required tool sets. Context is the serialized tool-definition character count divided by four. It excludes the user/system prompt and is an estimate, not tokenizer output.
- Retrieval and end-to-end timings are separate. End-to-end includes fixture execution and graph trace writes. Local hardware, warm caches, and 15-sample p95 values limit generalization.

## Results

Each row reports success rate, required-tool recall, estimated tool-definition tokens, unnecessary selected tools per task, retrieval p50/p95 milliseconds, and end-to-end p50/p95 milliseconds.

| Catalog | Strategy | Success | Recall | Est. tokens | Unnecessary tools | Retrieval p50/p95 ms | End-to-end p50/p95 ms |
|---:|---|---:|---:|---:|---:|---:|---:|
| 20 | Full context | 100% | 100% | 1,831 | 15.7 | 0.0 / 0.0 | 11.1 / 11.8 |
| 20 | Flat keyword | 0% | 30% | 747 | 6.7 | 0.1 / 0.2 | 11.0 / 12.0 |
| 20 | HydraDB ablation | 0% | 30% | 747 | 6.7 | 4.3 / 5.2 | 10.5 / 12.3 |
| 20 | HydraDB graph | 100% | 100% | 427 | 1.7 | 16.1 / 26.9 | 24.7 / 38.9 |
| 100 | Full context | 100% | 100% | 8,651 | 95.7 | 0.0 / 0.0 | 10.9 / 11.3 |
| 100 | Flat keyword | 0% | 30% | 747 | 6.7 | 0.5 / 0.5 | 10.3 / 17.2 |
| 100 | HydraDB ablation | 0% | 30% | 747 | 6.7 | 18.1 / 19.1 | 33.3 / 34.1 |
| 100 | HydraDB graph | 100% | 100% | 427 | 1.7 | 19.1 / 19.7 | 33.6 / 34.8 |
| 500 | Full context | 100% | 100% | 42,751 | 495.7 | 0.0 / 0.0 | 10.5 / 11.5 |
| 500 | Flat keyword | 0% | 30% | 747 | 6.7 | 2.4 / 2.8 | 19.0 / 21.9 |
| 500 | HydraDB ablation | 0% | 30% | 747 | 6.7 | 96.6 / 129.3 | 115.8 / 142.4 |
| 500 | HydraDB graph | 100% | 100% | 427 | 1.7 | 22.6 / 29.9 | 35.4 / 40.6 |

Graph selection reduces estimated tool-definition context relative to full context by 76.7% at 20 tools, 95.1% at 100, and 99.0% at 500. Full context is faster in this local deterministic setup because selection is already an in-memory list. Graph routing costs more retrieval time, and at 500 tools its median retrieval time is 22.6 ms; the database scan ablation grows to 96.6 ms. The context/success tradeoff is the supported result here, not a universal latency win.

The flat ranker often finds one lexical match but misses required dependency tools and entity resolvers. The ablation confirms that loading the HydraDB catalog without traversing its relationships does not recover those paths. These tasks and routing fixtures are intentionally narrow; other catalogs or stronger flat/embedding rankers may perform differently.

## Feedback evaluation

The benchmark resets each graph before evaluation. At each of the three catalog sizes, the held-out accuracy, task success, and required-tool recall were 33.3% at iteration 0, then 100% after the first training feedback cycle, and remained 100% through iterations 2 and 3. The initial weighted skill edges are tied. Three training prompts reinforce the appropriate skill edges; the three held-out evaluation prompts use different wording and entity IDs and never update the graph. Estimated context in this evaluation was 463 tokens initially and 465 tokens after routing learned the broader support skills.

This is a small deterministic test set. It demonstrates the feedback mechanism and split discipline, not statistical generalization. Raw per-request data is created in ignored `results/latest.json` by rerunning `scripts/benchmark.py`.

## Concurrency check

The 500-tool local graph route also completed 30 concurrent API requests at concurrency 8 with 30 successful workflows and no errors. The fresh run reported request p50 87.3 ms, p95 163.0 ms, and mean 99.2 ms. This is a smoke load check on one local process, not a capacity or availability claim.
