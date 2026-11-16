"""Context retrieval methods and deterministic execution for the playground."""
from __future__ import annotations

import re
import time
from typing import Any

from .fixtures import CORE_TOOLS, DATA_TOOLS, SKILLS, TASKS, Tool, make_catalog
from .hydra import HydraClient, cypher_string


def classify(text: str) -> str:
    value = text.lower()
    if any(word in value for word in ("refund", "eligible", "qualify")):
        return "refund"
    if any(word in value for word in ("invoice", "payment", "subscription", "charge", "billing")):
        return "billing"
    if any(word in value for word in ("delivery", "shipment", "late", "carrier", "package", "return status", "return state", "return authorization")):
        return "delivery"
    return "refund"


def terms(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9_.-]+", text.lower()) if len(w) > 2}


def flat_retrieve(text: str, catalog: list[Tool], limit: int = 8) -> list[Tool]:
    query_terms = terms(text)
    def score(tool: Tool) -> tuple[int, str]:
        corpus = terms(f"{tool.key} {tool.name} {tool.description} {tool.server} {tool.skill} {tool.input_schema}")
        return (len(query_terms & corpus), tool.key)
    return sorted(catalog, key=score, reverse=True)[:limit]


def expanded_flat_retrieve(text: str, catalog: list[Tool], limit: int = 5) -> list[Tool]:
    """Lexical top-k retrieval followed by declared dependency expansion."""
    by_key = {tool.key: tool for tool in catalog}
    chosen = {tool.key: tool for tool in flat_retrieve(text, catalog, limit)}
    frontier = list(chosen)
    while frontier:
        key = frontier.pop()
        for dependency in by_key[key].dependencies:
            if dependency in by_key and dependency not in chosen:
                chosen[dependency] = by_key[dependency]
                frontier.append(dependency)
    return list(chosen.values())


def tool_json(tool: Tool) -> dict[str, Any]:
    return {"key": tool.key, "name": tool.name, "description": tool.description, "server": tool.server, "skill": tool.skill, "input_schema": tool.input_schema}


SKILL_TERMS = {
    "customer_context": {"customer", "shopper", "buyer", "account", "orders", "history", "purchases", "who", "belongs"},
    "order_support": {"order", "purchase", "total", "details", "status"},
    "billing_review": {"payment", "payments", "charge", "charged", "paid", "installments", "transaction", "amount", "money"},
    "delivery_investigation": {"delivery", "delivered", "shipment", "shipping", "late", "arrived", "carrier", "estimated", "received", "parcel"},
    "product_research": {"product", "products", "category", "seller", "merchant", "weight", "item", "items", "line", "lines", "goods"},
    "refund_review": {"refund", "eligible", "eligibility", "return", "reimburse", "policy"},
}


def candidate_skills(text: str, limit: int = 3) -> list[dict[str, Any]]:
    """Local semantic-ish candidate generation, shared by graph and flat routes."""
    words = terms(text)
    scores = []
    for skill, vocabulary in SKILL_TERMS.items():
        matched = sorted(words & vocabulary)
        if matched:
            scores.append({"skill": skill, "score": len(matched), "evidence": matched})
    scores.sort(key=lambda row: (-row["score"], row["skill"]))
    if not scores and re.search(r"\b[0-9a-f]{32}\b", text, re.I):
        scores = [{"skill": "order_support", "score": 1, "evidence": ["order identifier"]}]
    return scores[:limit]


def flat_candidates(task: str, candidates: list[dict[str, Any]], expand: bool = False) -> list[Tool]:
    pool = [tool for tool in DATA_TOOLS if tool.skill in {row["skill"] for row in candidates}]
    return expanded_flat_retrieve(task, pool, limit=4) if expand else flat_retrieve(task, pool, limit=6)


async def route_graph_candidates(db: HydraClient, candidates: list[dict[str, Any]], order_id: str | None = None, max_depth: int = 6,
                                exclude_dependencies: set[tuple[str, str]] | None = None,
                                root_tool_keys: set[str] | None = None) -> dict[str, Any]:
    """HydraDB skill roots plus bounded, cycle-safe transitive tool closure."""
    if not candidates:
        return {"skills": [], "tools": [], "edges": [], "resolved_entities": [], "retrieval_ms": 0.0}
    start = time.perf_counter()
    skill_ids = [db.id_for("Skill", row["skill"]) for row in candidates]
    ids = ",".join(str(i) for i in skill_ids)
    _, roots, _ = await db.query(
        f"MATCH (s:Skill)-[:USES]->(t:Tool) WHERE s.id IN [{ids}] OPTIONAL MATCH (t)-[:PROVIDED_BY]->(m:MCPServer) "
        "RETURN DISTINCT s.key AS skill,t.id AS id,t.key AS key,t.name AS name,t.description AS description,t.server AS server,t.schema AS schema,m.key AS mcp ORDER BY key"
    )
    chosen: dict[int, dict[str, Any]] = {}
    edges: list[dict[str, str]] = []
    for skill, ident, key, name, description, server, schema, mcp in roots:
        if root_tool_keys is not None and key not in root_tool_keys:
            continue
        ident = int(ident)
        if ident in chosen:
            chosen[ident]["root_skills"].append(skill)
        else:
            chosen[ident] = {"id": ident, "key": key, "name": name, "description": description, "server": server, "schema": schema, "mcp": mcp, "root_skills": [skill]}
        edges.append({"from": skill, "to": key, "type": "USES"})
        if mcp: edges.append({"from": key, "to": mcp, "type": "PROVIDED_BY"})
    frontier, visited = set(chosen), set(chosen)
    for _ in range(max_depth):
        if not frontier: break
        ids = ",".join(str(i) for i in sorted(frontier))
        excluded = "".join(f" AND NOT (a.key = '{a}' AND b.key = '{b}')" for a,b in sorted(exclude_dependencies or set()))
        _, rows, _ = await db.query(
            f"MATCH (a:Tool)-[r:DEPENDS_ON]->(b:Tool) WHERE a.id IN [{ids}] AND (r.weight IS NULL OR r.weight > 0){excluded} "
            "OPTIONAL MATCH (b)-[:PROVIDED_BY]->(m:MCPServer) "
            "RETURN a.id AS src_id,a.key AS src,b.id AS id,b.key AS key,b.name AS name,b.description AS description,b.server AS server,b.schema AS schema,m.key AS mcp ORDER BY key"
        )
        next_frontier = set()
        for src_id, src, ident, key, name, description, server, schema, mcp in rows:
            ident, src_id = int(ident), int(src_id)
            edges.append({"from": src, "to": key, "type": "DEPENDS_ON"})
            if ident not in chosen:
                chosen[ident] = {"id": ident, "key": key, "name": name, "description": description, "server": server, "schema": schema, "mcp": mcp, "root_skills": []}
            if ident not in visited: next_frontier.add(ident)
        visited.update(next_frontier); frontier = next_frontier
    resolved = []
    if order_id:
        order_node = db.id_for("Entity", f"order:{order_id}")
        _, entity_rows, _ = await db.query(
            f"MATCH (o:Entity {{id: {order_node}}})-[:BELONGS_TO]->(c:Entity) "
            "OPTIONAL MATCH (o)-[:HAS_ITEM]->(i:Entity)-[:ITEM_PRODUCT]->(p:Entity) "
            "OPTIONAL MATCH (i)-[:SOLD_BY]->(s:Entity) "
            "RETURN o.external_id AS order,c.external_id AS customer,p.external_id AS product,s.external_id AS seller"
        )
        if entity_rows:
            vals = entity_rows[0]
            resolved.append({"type": "order", "id": vals[0]})
            if vals[1]: resolved.append({"type": "customer", "id": vals[1]})
            for row in entity_rows:
                if row[2]: resolved.append({"type": "product", "id": row[2]})
                if row[3]: resolved.append({"type": "seller", "id": row[3]})
            unique = {(r["type"], r["id"]): r for r in resolved}
            resolved = list(unique.values())
            edges.append({"from": f"order:{order_id}", "to": f"customer:{vals[1]}", "type": "BELONGS_TO"})
    candidate_map = {row["skill"]: row for row in candidates}
    for tool in chosen.values():
        root_skills = tool["root_skills"]
        tool["why"] = [f"Skill {skill} matched task terms: {', '.join(candidate_map[skill]['evidence'])}" for skill in root_skills if skill in candidate_map]
        if not root_skills:
            parents = [edge["from"] for edge in edges if edge["to"] == tool["key"] and edge["type"] == "DEPENDS_ON"]
            tool["why"] = [f"Required dependency of {parent}" for parent in parents]
    return {"skills": candidates, "tools": list(chosen.values()), "edges": edges, "resolved_entities": resolved, "retrieval_ms": (time.perf_counter()-start)*1000}


async def route_graph(db: HydraClient, scenario: str, task_text: str = "") -> dict[str, Any]:
    task_id = db.id_for("TaskType", scenario)
    t0 = time.perf_counter()
    _, ranks, _ = await db.query(
        f"MATCH (tt:TaskType {{id: {task_id}}})-[r:REQUIRES]->(s:Skill) "
        "RETURN s.id AS id, s.key AS key, r.weight AS weight ORDER BY weight DESC, key ASC LIMIT 1"
    )
    if not ranks:
        raise RuntimeError(f"No TaskType-to-Skill route is registered for {scenario}")
    skill_id, skill, skill_weight = ranks[0]
    _, roots, root_latency = await db.query(
        f"MATCH (tt:TaskType {{id: {task_id}}})-[:REQUIRES]->(s:Skill {{id: {int(skill_id)}}})-[:USES]->(t:Tool) "
        "OPTIONAL MATCH (t)-[:PROVIDED_BY]->(m:MCPServer) "
        "RETURN DISTINCT s.key AS skill, t.id AS id, t.key AS key, t.name AS name, t.description AS description, t.server AS server, t.schema AS schema, m.key AS mcp "
        "ORDER BY key"
    )
    selected: dict[int, dict[str, Any]] = {}
    root_ids: set[int] = set()
    edges: list[dict[str, str]] = [{"from": scenario, "to": skill, "type": "REQUIRES"}]
    for row in roots:
        skill_key, ident, key, name, description, server, schema, mcp = row
        ident = int(ident); root_ids.add(ident)
        selected[ident] = {"id": ident, "key": key, "name": name, "description": description, "server": server, "schema": schema, "mcp": mcp}
        edges.append({"from": skill_key, "to": key, "type": "USES"})
        if mcp:
            edges.append({"from": key, "to": mcp, "type": "PROVIDED_BY"})

    # Bounded dependency expansion with a visited set. Each wave is a genuine
    # HydraDB relationship query; cycles and repeated dependencies are guarded.
    frontier = set(root_ids)
    visited = set(root_ids)
    traversal_ms = (time.perf_counter() - t0) * 1000
    for _depth in range(4):
        if not frontier:
            break
        ids = ", ".join(str(i) for i in sorted(frontier))
        query_start = time.perf_counter()
        _, dependencies, _ = await db.query(
            f"MATCH (a:Tool)-[:DEPENDS_ON]->(b:Tool) WHERE a.id IN [{ids}] OPTIONAL MATCH (b)-[:PROVIDED_BY]->(m:MCPServer) "
            "RETURN a.id AS src_id, a.key AS src, b.id AS id, b.key AS key, b.name AS name, b.description AS description, b.server AS server, b.schema AS schema, m.key AS mcp"
        )
        traversal_ms += (time.perf_counter() - query_start) * 1000
        next_frontier: set[int] = set()
        for src_id, src, ident, key, name, description, server, schema, mcp in dependencies:
            ident = int(ident); src_id = int(src_id)
            edges.append({"from": src, "to": key, "type": "DEPENDS_ON"})
            if ident not in selected:
                selected[ident] = {"id": ident, "key": key, "name": name, "description": description, "server": server, "schema": schema, "mcp": mcp}
            if mcp:
                edges.append({"from": key, "to": mcp, "type": "PROVIDED_BY"})
            if ident not in visited:
                next_frontier.add(ident)
        visited.update(next_frontier)
        frontier = next_frontier

    entity_ids = {kind: ident for kind, ident in re.findall(r"\b(T|C|O)-([A-Za-z0-9]+)\b", task_text, re.I)}
    prefixes = {"T": "ticket", "C": "customer", "O": "order"}
    requested = {prefixes[k.upper()]: f"{k.upper()}-{v}" for k, v in entity_ids.items()}
    # Use only validated alphanumeric IDs parsed from the user's text. When an
    # ID is provided, it must match the entity path exactly.
    if requested:
        ticket_id = db.id_for("Entity", f"ticket:{requested.get('ticket', '')}") if "ticket" in requested else None
        customer_id = db.id_for("Entity", f"customer:{requested.get('customer', '')}") if "customer" in requested else None
        order_id = db.id_for("Entity", f"order:{requested.get('order', '')}") if "order" in requested else None
        anchors = []
        if ticket_id is not None: anchors.append(f"ticket.id = {ticket_id}")
        if customer_id is not None: anchors.append(f"customer.id = {customer_id}")
        if order_id is not None: anchors.append(f"order.id = {order_id}")
        where = " AND ".join(anchors)
        _, entities, entity_ms = await db.query(
            "MATCH (ticket:Entity)-[:IDENTIFIES]->(customer:Entity)-[:HAS_ORDER]->(order:Entity) "
            f"WHERE {where} RETURN ticket.external_id AS ticket, customer.external_id AS customer, order.external_id AS order LIMIT 1"
        )
    else:
        entities, entity_ms = [], 0.0
    resolved = []
    if entities:
        ticket, customer, order = entities[0]
        # Check all explicitly requested IDs, including formats that may have
        # resolved to an ID with no matching graph node.
        if any(requested.get(kind) != actual for kind, actual in [("ticket", ticket), ("customer", customer), ("order", order)] if kind in requested):
            entities = []
    if entities:
        ticket, customer, order = entities[0]
        resolved = [
            {"type": "ticket", "id": ticket},
            {"type": "customer", "id": customer},
            {"type": "order", "id": order},
        ]
        edges.extend([
            {"from": scenario, "to": f"ticket:{ticket}", "type": "HAS_CONTEXT"},
            {"from": f"ticket:{ticket}", "to": f"customer:{customer}", "type": "IDENTIFIES"},
            {"from": f"customer:{customer}", "to": f"order:{order}", "type": "HAS_ORDER"},
        ])
        selected = {ident: tool for ident, tool in selected.items() if tool["key"] not in {"customer.resolve", "orders.list"}}
    traversal_ms += entity_ms
    return {"skill": skill, "skill_weight": skill_weight, "tools": list(selected.values()), "edges": edges, "resolved_entities": resolved, "retrieval_ms": traversal_ms}


def execute_fixture(scenario: str, method: str, tool_keys: set[str], resolved: list[dict[str, str]]) -> dict[str, Any]:
    fixture = TASKS[scenario]
    reused = ["customer.resolve", "orders.list"] if any(e["type"] == "customer" for e in resolved) else []
    needed = [key for key in fixture["required"] if key not in reused]
    trace = [{"step": "retrieve", "message": f"{len(tool_keys)} tool definitions retrieved via {method}"}]
    outputs: list[dict[str, Any]] = []
    if resolved:
        trace.append({"step": "entity-reuse", "message": f"Reused resolved entities: {', '.join(e['id'] for e in resolved)}"})
    missing: list[str] = []
    calls = 0
    completed = set(reused)
    dependency_failures: list[str] = []
    dependencies = {tool.key: tool.dependencies for tool in CORE_TOOLS}
    tool_results = {
        "ticket.lookup": {"ticket_id": fixture["ticket"], "customer_id": fixture["customer"], "issue": "refund request / order support"},
        "customer.resolve": {"customer_id": fixture["customer"], "canonical": True},
        "customer.profile": {"customer_id": fixture["customer"], "tier": "standard", "status": "active"},
        "orders.list": {"customer_id": fixture["customer"], "orders": [fixture["order"]]},
        "order.get": {"order_id": fixture["order"], "status": "delivered", "total": 84.50},
        "refund.eligibility": {"order_id": fixture["order"], "eligible": fixture["eligible"], "policy_reason": "within policy window" if fixture["eligible"] else "outside policy window"},
        "invoice.lookup": {"customer_id": fixture["customer"], "invoice_id": "I-3401", "balance": 0},
        "payment.history": {"customer_id": fixture["customer"], "events": ["authorized", "captured"]},
        "shipment.track": {"order_id": fixture["order"], "last_scan": "regional hub", "eta": "2026-11-16"},
        "return.status": {"order_id": fixture["order"], "return_authorized": False},
    }
    for key in fixture["required"]:
        if key in reused:
            trace.append({"step": key, "message": f"{key}: reused from HydraDB entity context"})
            continue
        if key not in tool_keys:
            missing.append(key)
            trace.append({"step": key, "message": f"{key}: unavailable in retrieved tool context"})
            continue
        blocked_by = [dep for dep in dependencies.get(key, ()) if dep not in completed]
        if blocked_by:
            dependency_failures.extend(blocked_by)
            trace.append({"step": key, "message": f"{key}: blocked by unresolved dependencies {', '.join(blocked_by)}"})
            continue
        result = tool_results.get(key, {"ok": True, "action": key, "mode": "deterministic local fixture"})
        outputs.append({"tool": key, "result": result})
        completed.add(key)
        calls += 1
        trace.append({"step": key, "message": f"{key}: executed successfully", "result": result})
    success = not missing and not dependency_failures
    if not success:
        answer = "The available context is missing required tools, so the task could not be completed safely."
    elif scenario == "refund":
        eligible = next((row["result"]["eligible"] for row in outputs if row["tool"] == "refund.eligibility"), False)
        answer = f"Ticket {fixture['ticket']} maps to customer {fixture['customer']} and order {fixture['order']}. " + ("The order is eligible for a refund; no refund was issued." if eligible else "The order is not eligible under the current policy. No refund was issued.")
    elif scenario == "billing":
        balance = next((row["result"]["balance"] for row in outputs if row["tool"] == "invoice.lookup"), None)
        answer = f"Ticket {fixture['ticket']} maps to customer {fixture['customer']}. The invoice lookup shows a current balance of {balance}; payment history shows the charge was captured."
    else:
        scan = next((row["result"]["last_scan"] for row in outputs if row["tool"] == "shipment.track"), "unknown")
        answer = f"Ticket {fixture['ticket']} maps to customer {fixture['customer']} and order {fixture['order']}. The latest carrier scan is {scan}. The return status was checked; no write action was taken."
    return {"success": success, "missing_tools": missing, "dependency_failures": dependency_failures, "trace": trace, "answer": answer, "tool_calls": calls, "tool_outputs": outputs, "reused_entities": resolved}
