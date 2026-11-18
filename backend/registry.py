"""Bulk catalog loader using HydraDB's supported OpenCypher write surface."""
from __future__ import annotations

from collections import defaultdict
import json
from typing import Any

from .fixtures import SERVERS, SKILLS, TASKS, Tool, make_catalog
from .hydra import HydraClient
from .olist_store import DEFAULT_DB, available, connect


def node(kind: str, key: str, **props: Any) -> dict[str, Any]:
    return {"id": HydraClient.id_for(kind, key), "key": key, **props}


def relationship(src_kind: str, src: str, dst_kind: str, dst: str, **props: Any) -> dict[str, Any]:
    return {"src": HydraClient.id_for(src_kind, src), "dst": HydraClient.id_for(dst_kind, dst), **props}


async def seed_catalog(db: HydraClient, size: int = 20, reset: bool = True, catalog_scope: str = "combined") -> dict[str, Any]:
    catalog = make_catalog(size, scope=catalog_scope)
    if reset:
        for label in ("Execution", "LearningUpdate", "TaskType", "Agent", "Skill", "Tool", "MCPServer", "Entity", "Task"):
            await db.query(f"MATCH (n:{label}) DETACH DELETE n")

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    edges: dict[str, list[dict[str, Any]]] = defaultdict(list)

    agent = node("Agent", "support-agent", name="Support operations agent", description="Resolves customer context and investigates support requests.")
    grouped["Agent"].append(agent)
    active_skills = {tool.skill for tool in catalog}
    for fixture in TASKS.values(): active_skills.update(fixture["skills"])
    for key, (name, description) in SKILLS.items():
        if key not in active_skills: continue
        grouped["Skill"].append(node("Skill", key, name=name, description=description))
        edges["HAS_SKILL"].append(relationship("Agent", "support-agent", "Skill", key, weight=1.0))

    for server_id, server_name in SERVERS.items():
        if any(tool.server == server_id for tool in catalog):
            grouped["MCPServer"].append(node("MCPServer", server_id, name=server_name, transport="stdio", protocol="MCP"))

    for tool in catalog:
        grouped["Tool"].append(node("Tool", tool.key, name=tool.name, description=tool.description, server=tool.server, skill=tool.skill, schema=tool.input_schema))
        edges["USES"].append(relationship("Skill", tool.skill, "Tool", tool.key, weight=1.0))
        edges["PROVIDED_BY"].append(relationship("Tool", tool.key, "MCPServer", tool.server))
        for entity_kind in tool.entities:
            entity_key = {"customer": "customer:C-1042", "ticket": "ticket:T-2049", "order": "order:O-8891", "invoice": "invoice:I-3401"}[entity_kind]
            grouped["Entity"].append(node("Entity", entity_key, entity_type=entity_kind, external_id=entity_key.split(":", 1)[1], name=entity_kind.title()))
            edges["OPERATES_ON"].append(relationship("Tool", tool.key, "Entity", entity_key))
        for dep in tool.dependencies:
            edges["DEPENDS_ON"].append(relationship("Tool", tool.key, "Tool", dep, weight=1.0))

    # Dedupe nodes and relationships before the bulk UNWIND writes.
    for label, rows in list(grouped.items()):
        unique = {row["id"]: row for row in rows}
        grouped[label] = list(unique.values())
    for rel_type, rows in list(edges.items()):
        unique = {(row["src"], row["dst"]): row for row in rows}
        edges[rel_type] = list(unique.values())

    for scenario, fixture in TASKS.items():
        grouped["TaskType"].append(node("TaskType", scenario, name=fixture["title"], description=fixture["task"]))
        for skill in fixture["skills"]:
            edges["REQUIRES"].append(relationship("TaskType", scenario, "Skill", skill, weight=1.0))
        ticket = f"ticket:{fixture['ticket']}"
        customer = f"customer:{fixture['customer']}"
        order = f"order:{fixture['order']}"
        for key, kind, external in [(ticket, "ticket", fixture["ticket"]), (customer, "customer", fixture["customer"]), (order, "order", fixture["order"])]:
            grouped["Entity"].append(node("Entity", key, entity_type=kind, external_id=external, name=f"Resolved {kind}"))
        edges["HAS_CONTEXT"].append(relationship("TaskType", scenario, "Entity", ticket))
        edges["IDENTIFIES"].append(relationship("Entity", ticket, "Entity", customer))
        edges["HAS_ORDER"].append(relationship("Entity", customer, "Entity", order))

    for label, rows in grouped.items():
        # Rows are small enough for HydraDB's bounded request size even at 500 tools.
        props = sorted({k for row in rows for k in row} - {"id"})
        assignments = [f"n.{prop} = row.{prop}" for prop in props]
        set_clause = f" SET n:{label}" + (", " + ", ".join(assignments) if assignments else "")
        await db.query(f"UNWIND $rows AS row MERGE (n {{id: row.id}}){set_clause}", {"rows": rows})

    for rel_type, rows in edges.items():
        await db.query(f"UNWIND $rows AS row CREATE (a {{id: row.src}})-[:{rel_type}]->(b {{id: row.dst}})", {"rows": rows})

    for scenario, fixture in TASKS.items():
        task_id = HydraClient.id_for("TaskType", scenario)
        for skill in fixture["skills"]:
            skill_id = HydraClient.id_for("Skill", skill)
            await db.query(f"MATCH (t:TaskType {{id: {task_id}}})-[r:REQUIRES]->(s:Skill {{id: {skill_id}}}) SET r.weight = 1.0")

    data_graph = await seed_olist_entities(db, DEFAULT_DB) if catalog_scope == "combined" and available(DEFAULT_DB) else {"entities": 0, "entity_edges": 0}

    # Add an Agent -> Skill -> Tool -> MCP sample path as the model's front door.
    # Agent -> skill edges are already registered above.
    return {"catalog_scope": catalog_scope, "tools": len(catalog), "skills": len(active_skills), "servers": len(grouped["MCPServer"]), "task_types": len(TASKS), "nodes": sum(map(len, grouped.values())) + data_graph["entities"], "relationship_types": len(edges), **data_graph}


async def seed_olist_entities(db: HydraClient, path=DEFAULT_DB, limit: int = 1000) -> dict[str, int]:
    """Seed only anonymized IDs and Olist relationships; facts stay in SQLite."""
    with connect(path) as conn:
        orders = [dict(r) for r in conn.execute("SELECT order_id,customer_id FROM orders ORDER BY order_id LIMIT ?", (limit,))]
        order_ids = [r["order_id"] for r in orders]
        customers = {r["customer_id"]: r["customer_unique_id"] for r in conn.execute("SELECT customer_id,customer_unique_id FROM customers")}
        placeholders = ",".join("?" for _ in order_ids)
        items = [dict(r) for r in conn.execute(f"SELECT order_id,item_no,product_id,seller_id FROM order_items WHERE order_id IN ({placeholders})", order_ids)] if order_ids else []
    nodes: dict[str, dict] = {}
    links: dict[str, list[dict]] = defaultdict(list)
    for order in orders:
        oid, cid = order["order_id"], order["customer_id"]
        customer_ref = customers.get(cid)
        nodes[f"order:{oid}"] = node("Entity", f"order:{oid}", entity_type="order", external_id=oid, name="Olist order", source="olist")
        if customer_ref:
            nodes[f"customer:{customer_ref}"] = node("Entity", f"customer:{customer_ref}", entity_type="customer", external_id=customer_ref, name="Anonymized Olist customer", source="olist")
            links["BELONGS_TO"].append(relationship("Entity", f"order:{oid}", "Entity", f"customer:{customer_ref}"))
    for item in items:
        item_key = f"item:{item['order_id']}:{item['item_no']}"
        product_key, seller_key = f"product:{item['product_id']}", f"seller:{item['seller_id']}"
        nodes[item_key] = node("Entity", item_key, entity_type="order_item", external_id=f"{item['order_id']}:{item['item_no']}", name="Olist order item", source="olist")
        nodes[product_key] = node("Entity", product_key, entity_type="product", external_id=item["product_id"], name="Olist product", source="olist")
        nodes[seller_key] = node("Entity", seller_key, entity_type="seller", external_id=item["seller_id"], name="Olist seller", source="olist")
        links["HAS_ITEM"].append(relationship("Entity", f"order:{item['order_id']}", "Entity", item_key))
        links["ITEM_PRODUCT"].append(relationship("Entity", item_key, "Entity", product_key))
        links["SOLD_BY"].append(relationship("Entity", item_key, "Entity", seller_key))
    rows = list(nodes.values())
    for offset in range(0, len(rows), 250):
        chunk = rows[offset:offset + 250]
        await db.query("UNWIND $rows AS row MERGE (n {id: row.id}) SET n:Entity, n.key=row.key, n.entity_type=row.entity_type, n.external_id=row.external_id, n.name=row.name, n.source=row.source", {"rows": chunk})
    edge_count = 0
    for rel, links_rows in links.items():
        unique = {(r["src"], r["dst"]): r for r in links_rows}
        rows = list(unique.values())
        for offset in range(0, len(rows), 250):
            await db.query(f"UNWIND $rows AS row CREATE (a {{id: row.src}})-[:{rel}]->(b {{id: row.dst}})", {"rows": rows[offset:offset + 250]})
        edge_count += len(rows)
    return {"entities": len(nodes), "entity_edges": edge_count}


async def inspect_tools(db: HydraClient) -> list[dict[str, Any]]:
    _, rows, _ = await db.query("MATCH (t:Tool) RETURN t.key AS key, t.name AS name, t.description AS description, t.server AS server, t.schema AS schema, t.provenance AS provenance, t.catalog AS catalog, t.output_schema AS output_schema ORDER BY t.key")
    return [dict(zip(["key", "name", "description", "server", "schema", "provenance", "catalog", "output_schema"], row)) for row in rows]
