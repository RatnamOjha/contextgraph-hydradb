# SDK retrieval evaluation methodology

`ContextGraph.evaluate(catalog, tasks, max_tools=8)` evaluates retrieval output against developer-supplied labels. It does not execute tools or ask an LLM to perform tasks.

Each task contains text and a `required_tools` list of exact IDs in the selected catalog. For each task the evaluator calls graph retrieval and computes a flat lexical top-k result from the same listed catalog definitions and metadata. Both strategies use the same maximum tool count. The flat baseline does not expand graph dependency edges.

## Reported measurements

- **Required-tool recall:** fraction of labeled required IDs in the selected set.
- **Retrieved tool count:** number of returned tool definitions.
- **Dependency completeness:** whether prerequisites declared in the graph are available or validated as known context.
- **Unrelated tool exposure:** retrieved IDs absent from the caller-supplied required list. This is a proxy based on those labels, not a universal judgment of relevance.
- **Estimated schema tokens:** serialized schemas and metadata character count divided by four and rounded up.
- **Retrieval latency:** measured wall-clock time for the retrieval operation.

The graph strategy's dependency expansion can increase retrieved count or estimated context when a required prerequisite is useful. A flat result can have high required-tool recall and still omit prerequisites. Review per-task results and negative controls; averages can hide either outcome.

## What results do not show

The evaluator does not establish task completion, argument quality, tool execution success, agent accuracy, semantic relevance, actual model tokenization, provider billing, or token savings. Tool labels are supplied by the developer and are not independently validated. The character-based token estimate is only a consistent rough comparison. Retrieval timing excludes the developer's model and tool execution.

The reproducible isolated synthetic-company benchmark in `docs/DEMO_COMPANY_BENCHMARK.md` is a separate benchmark and should not be conflated with this SDK evaluator. Olist demo evaluations and real-Groq pilot results are also separate; see `docs/REAL_AGENT_BENCHMARK.md` for real-agent caveats.
