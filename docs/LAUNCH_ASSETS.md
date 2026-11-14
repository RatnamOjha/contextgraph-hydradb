# Launch assets

## Product line

**ContextGraph: more tools, smaller agent context.** Give an agent only the MCP tools and known entities connected to its task, with dependencies kept in execution order.

## Audience and wedge

Start with teams that maintain internal support, IT, or operations MCP catalogs. Their catalogs grow faster than the practical tool context an agent can safely handle. The first use case is support ticket resolution with dependent customer, order, shipment, and billing tools. Validate willingness to pay in customer interviews; the current demo does not establish pricing or market size.

## Positioning against current frameworks

MCP and agent frameworks already handle tool discovery and invocation. For example, the MCP TypeScript SDK exposes `listTools()` and `callTool()`, and its getting-started flow shows the full list of tool name, description, and input schema passed through to a model. ContextGraph does not replace that protocol or the agent loop. It adds a persistent graph that models task-to-skill fit, cross-tool prerequisites, server ownership, and resolved-entity links, then returns a task-specific candidate set. The gap is not “frameworks cannot use tools”; it is that each application still needs a strategy for selecting a safe, dependency-complete subset from a large, distributed catalog and reusing state across tasks.

This distinction is a product hypothesis. Existing applications can build their own routing, filters, or dynamic tool selection, and the benchmark does not compare against a mature semantic or framework-native selector. A real integration should test whether ContextGraph improves selection quality enough to justify a network hop and catalog maintenance.

References: [MCP TypeScript SDK tool listing and invocation](https://ts.sdk.modelcontextprotocol.io/v2/clients/calling), [MCP TypeScript SDK client handoff](https://ts.sdk.modelcontextprotocol.io/v2/get-started/first-client.html).

## Integration and pricing hypothesis

The integration point is between an MCP registry and an existing agent runtime: ingest tool metadata and declared dependencies, resolve a task to a compact candidate set, pass those schemas to the existing model/tool loop, and persist successful paths and reusable entities. A developer tier could be free for local/small catalogs, with a paid team subscription for hosted graph capacity, private catalogs, access controls, audit traces, and support. Price should be tested in interviews against catalog size, tool-call volume, and required deployment model; no amount is claimed from this prototype.

## 30-second pitch

“Every MCP tool definition you send an agent consumes context, even when the task needs only a few. ContextGraph stores skills, tools, dependencies, and resolved entities in HydraDB, then traverses the relevant neighborhood for the current task. In our support demo, it selects a compact, ordered tool set and reuses known ticket context. We measure success, recall, context size, and latency against full-context, keyword, and graph-scan baselines.”

## Two-minute demo script

1. Show the 500-tool catalog and explain that most tools are irrelevant to any one support request.
2. Submit: “Ticket T-2049 reports a late package; inspect the shipment scan and return status.”
3. Run the graph route. Point out the task-to-skill path, selected tools, dependency order, and resolved ticket/order entities.
4. Show the deterministic tool trace and answer. Call out that the tools are fixtures, while the catalog selection and graph reads/writes are live against HydraDB.
5. Compare full context, flat keyword retrieval, HydraDB label-scan ablation, and graph traversal. Show context tokens, recall, success, and latency together.
6. Open the learning view, run the held-out evaluation, and explain that distinct training prompts update weighted task-to-skill edges while evaluation prompts never enter the feedback update.
7. Close with the integration shape: agent runtime → ContextGraph → HydraDB → compact tool context and trace.

## Loom narration

“This is ContextGraph, a context layer for large MCP catalogs. Here are 500 registered tools. Instead of placing every definition into the model context, I’ll ask it to inspect a delayed shipment. The HydraDB graph finds the support skill, follows the tool dependencies, and reuses the ticket and order entities. The runtime returns a trace so we can see each step. On the comparison panel, graph routing keeps full recall and task success on these fixed fixtures while reducing the estimated tool-definition context versus loading the full catalog. The baseline is local keyword search and a HydraDB label-scan ablation. This demo uses deterministic tools, so its execution results are reproducible and do not depend on an LLM provider. The next integration step is connecting a real MCP registry and agent runtime.”

## Social post drafts

**X:** We built ContextGraph for MCP catalog sprawl. Store skills, tools, dependencies, and known entities in HydraDB; traverse the task neighborhood; send the agent a compact tool context. The local demo compares graph routing with full context and keyword baselines at 20, 100, and 500 tools. [repo link]

**LinkedIn:** Agent teams are adding tools faster than prompts can carry them. ContextGraph is an experimental context layer for MCP catalogs: it models skills, tools, dependencies, and resolved entities in HydraDB, then retrieves a task-specific neighborhood. Our reproducible local benchmark measures task success, tool recall, context estimate, and latency against full-context and keyword baselines. The demo is deterministic and the current results are limited to three fixed support tasks; we’re looking for teams willing to test it against real catalogs. [repo link]

## Claims to preserve

- Describe benchmark results as fixed-fixture local measurements, not a general model-quality result.
- Say “estimated tool-definition tokens” and name the character-count-divided-by-four method.
- The local demo executes deterministic fixture tools; it does not yet connect to live third-party MCP servers or call an LLM.
- Keep HydraDB's open-source engine distinct from any separately hosted HydraDB offering.
