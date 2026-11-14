"""Bulk catalog loader using HydraDB's supported OpenCypher write surface."""
from __future__ import annotations

from collections import defaultdict
import json
from typing import Any

from .fixtures import CORE_TOOLS, SERVERS, SKILLS, TASKS, Tool, make_catalog
from .hydra import HydraClient


def node(kind: str, key: str, **props: Any) -> dict[str, Any]:
    return {"id": HydraClient.id_for(kind, key), "key": key, **props}


def relationship(src_kind: str, src: str, dst_kind: str, dst: str, **props: Any) -> dict[str, Any]:
    return {"src": HydraClient.id_for(src_kind, src), "dst": HydraClient.id_for(dst_kind, dst), **props}


async def seed_catalog(db: HydraClient, size: int = 20, reset: bool = True) -> dict[str, int]:
    catalog = make_catalog(size)
    if reset:
        for label in ("Execution", "TaskType", "Agent", "Skill", "Tool", "MCPServer", "Entity", "Task"):
            await db.query(f"MATCH (n:{label}) DETACH DELETE n")

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    edges: dict[str, list[dict[str, Any]]] = defaultdict(list)

    agent = node("Agent", "support-agent", name="Support operations agent", description="Resolves customer context and investigates support requests.")
    grouped["Agent"].append(agent)
    for key, (name, description) in SKILLS.items():
        grouped["Skill"].append(node("Skill", key, name=name, description=description))
        edges["HAS_SKILL"].append(relationship("Agent", "support-agent", "Skill", key, weight=1.0))

    for server_id, server_name in SERVERS.items():
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
            edges["DEPENDS_ON"].append(relationship("Tool", tool.key, "Tool", dep))

    tool_keys = {tool.key for tool in catalog}
    for scenario, fixture in TASKS.items():
        # Workflow skills can span MCP servers: this records the cross-server
        # tools required for the task as part of the selected skill context.
        for key in fixture["required"]:
            if key in tool_keys:
                edges["USES"].append(relationship("Skill", fixture["skill"], "Tool", key))

    # Dedupe nodes and relationships before the bulk UNWIND writes.
    for label, rows in list(grouped.items()):
        unique = {row["id"]: row for row in rows}
        grouped[label] = list(unique.values())
    for rel_type, rows in list(edges.items()):
        unique = {(row["src"], row["dst"]): row for row in rows}
        edges[rel_type] = list(unique.values())

    for scenario, fixture in TASKS.items():
        grouped["TaskType"].append(node("TaskType", scenario, name=fixture["title"], description=fixture["task"]))
        for skill in SKILLS:
            if skill == "catalog_utilities":
                continue
            edges["REQUIRES"].append(relationship("TaskType", scenario, "Skill", skill))
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

    for scenario in TASKS:
        task_id = HydraClient.id_for("TaskType", scenario)
        for skill in SKILLS:
            skill_id = HydraClient.id_for("Skill", skill)
            await db.query(f"MATCH (t:TaskType {{id: {task_id}}})-[r:REQUIRES]->(s:Skill {{id: {skill_id}}}) SET r.weight = 1.0")

    # Add an Agent -> Skill -> Tool -> MCP sample path as the model's front door.
    # Agent -> skill edges are already registered above.
    return {"tools": len(catalog), "skills": len(SKILLS), "servers": len(SERVERS), "task_types": len(TASKS), "nodes": sum(map(len, grouped.values())), "relationship_types": len(edges)}


async def inspect_tools(db: HydraClient) -> list[dict[str, Any]]:
    _, rows, _ = await db.query("MATCH (t:Tool) RETURN t.key AS key, t.name AS name, t.description AS description, t.server AS server, t.schema AS schema ORDER BY t.key")
    return [dict(zip(["key", "name", "description", "server", "schema"], row)) for row in rows]
