"""Safe, scoped registration primitives for developer-owned tool catalogs."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .hydra import HydraClient

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")


def catalog_fingerprint(tools: list[dict[str, Any]]) -> str:
    canonical = json.dumps(tools, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _tool_key(catalog: str, name: str) -> str:
    return f"{catalog}.{name}"


async def _ensure_edge(db: HydraClient, source_id: int, relation: str, target_id: int) -> None:
    # Relationship MERGE is not supported by the deployed Hydra query subset.
    # This route is loopback-only, so read-before-create keeps repeated local
    # registrations idempotent without using unsupported query syntax.
    _, rows, _ = await db.query(
        f"MATCH (a {{id: $source}})-[r:{relation}]->(b {{id: $target}}) RETURN a.id LIMIT 1",
        {"source": source_id, "target": target_id})
    if not rows:
        await db.query(f"UNWIND $rows AS row CREATE (a {{id: row.source}})-[:{relation}]->(b {{id: row.target}})",
            {"rows": [{"source": source_id, "target": target_id}]})


def validate_catalog_registration(catalog: str, tools: list[dict[str, Any]],
                                  existing_tool_names: set[str] | None = None) -> list[dict[str, Any]]:
    if not _IDENTIFIER.fullmatch(catalog):
        raise ValueError("catalog must be 1–80 letters, digits, dots, underscores, or hyphens")
    if not tools or len(tools) > 200:
        raise ValueError("tools must contain between 1 and 200 definitions")
    names: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for raw in tools:
        name = raw.get("name", "")
        if not isinstance(name, str) or not _IDENTIFIER.fullmatch(name):
            raise ValueError("each tool name must be a stable 1–80 character identifier")
        if name in names:
            raise ValueError(f"duplicate tool name in catalog: {name}")
        names.add(name)
        description = raw.get("description", "")
        if not isinstance(description, str) or len(description) > 4000:
            raise ValueError(f"description for {name} must be a string of at most 4000 characters")
        input_schema = raw.get("input_schema")
        output_schema = raw.get("output_schema")
        if input_schema is not None and not isinstance(input_schema, dict):
            raise ValueError(f"input_schema for {name} must be a JSON object")
        if output_schema is not None and not isinstance(output_schema, dict):
            raise ValueError(f"output_schema for {name} must be a JSON object")
        schemas = json.dumps({"input": input_schema, "output": output_schema}, separators=(",", ":"))
        if len(schemas) > 65536:
            raise ValueError(f"schemas for {name} exceed 64 KiB")
        server = raw.get("server") or catalog
        if not isinstance(server, str) or not _IDENTIFIER.fullmatch(server):
            raise ValueError(f"server for {name} must be a stable identifier")
        prerequisites = raw.get("prerequisites") or []
        if not isinstance(prerequisites, list) or any(not isinstance(dep, str) for dep in prerequisites):
            raise ValueError(f"prerequisites for {name} must be a list of tool names")
        if len(set(prerequisites)) != len(prerequisites) or name in prerequisites:
            raise ValueError(f"prerequisites for {name} contain duplicates or a self-reference")
        if any(not _IDENTIFIER.fullmatch(dep) for dep in prerequisites):
            raise ValueError(f"prerequisites for {name} must be tool names in the same catalog")
        normalized.append({"name": name, "description": description, "input_schema": input_schema,
            "output_schema": output_schema, "server": server, "prerequisites": prerequisites})
    # Do not create dangling dependency edges: every prerequisite must exist in
    # this registration batch or already be present in this same catalog.
    names = {tool["name"] for tool in normalized} | (existing_tool_names or set())
    for tool in normalized:
        unresolved = [dep for dep in tool["prerequisites"] if dep not in names]
        if unresolved:
            raise ValueError(f"unknown prerequisites for {tool['name']}: {', '.join(unresolved)}")
    return sorted(normalized, key=lambda tool: tool["name"])


async def register_catalog_tools(db: HydraClient, catalog: str, raw_tools: list[dict[str, Any]]) -> dict[str, Any]:
    _, existing_rows, _ = await db.query("MATCH (t:Tool) RETURN t.key AS key,t.catalog AS catalog")
    existing_names = {str(key)[len(catalog) + 1:] for key, scope in existing_rows
        if scope == catalog and str(key).startswith(catalog + ".")}
    tools = validate_catalog_registration(catalog, raw_tools, existing_names)
    fingerprint = catalog_fingerprint(tools)
    version = fingerprint[:16]
    catalog_id = HydraClient.id_for("Catalog", catalog)
    await db.query("UNWIND $rows AS row MERGE (c {id: row.id}) SET c:Catalog, c.key=row.catalog, c.name=row.catalog, c.fingerprint=row.fingerprint, c.version=row.version",
        {"rows": [{"id": catalog_id, "catalog": catalog, "fingerprint": fingerprint, "version": version}]})
    prepared: list[tuple[dict[str, Any], str, int]] = []
    for tool in tools:
        key = _tool_key(catalog, tool["name"])
        tool_id = HydraClient.id_for("Tool", key)
        server_key = f"{catalog}.{tool['server']}"
        server_id = HydraClient.id_for("MCPServer", server_key)
        encoded_input = json.dumps(tool["input_schema"], sort_keys=True, separators=(",", ":")) if tool["input_schema"] is not None else None
        encoded_output = json.dumps(tool["output_schema"], sort_keys=True, separators=(",", ":")) if tool["output_schema"] is not None else None
        assignments = ["t:Tool", "t.key=row.key", "t.name=row.name", "t.description=row.description",
            "t.server=row.server", "t.catalog=row.catalog", "t.provenance=row.provenance",
            "t.catalog_version=row.version", "t.catalog_fingerprint=row.fingerprint"]
        row = {"tool_id": tool_id, "key": key, "name": tool["name"], "description": tool["description"],
            "server": server_key, "catalog": catalog, "version": version, "fingerprint": fingerprint,
            "provenance": "developer_registered"}
        if encoded_input is not None:
            assignments.extend(["t.schema=row.input_schema", "t.input_schema=row.input_schema"])
            row["input_schema"] = encoded_input
        if encoded_output is not None:
            assignments.append("t.output_schema=row.output_schema")
            row["output_schema"] = encoded_output
        await db.query(
            "UNWIND $rows AS row MERGE (t {id: row.tool_id}) SET " + ", ".join(assignments),
            {"rows": [row]})
        removals = []
        if encoded_input is None:
            removals.extend(["t.schema", "t.input_schema"])
        if encoded_output is None:
            removals.append("t.output_schema")
        if removals:
            await db.query(f"MATCH (t:Tool {{id: $id}}) REMOVE {', '.join(removals)}", {"id": tool_id})
        await db.query("UNWIND $rows AS row MERGE (s {id: row.id}) SET s:MCPServer, s.key=row.server, s.name=row.server, s.catalog=row.catalog",
            {"rows": [{"id": server_id, "server": server_key, "catalog": catalog}]})
        await _ensure_edge(db, tool_id, "IN_CATALOG", catalog_id)
        await _ensure_edge(db, tool_id, "PROVIDED_BY", server_id)
        prepared.append((tool, key, tool_id))
    # Nodes are upserted before relationships so prerequisites may appear in
    # any order within the registration batch.
    for tool, key, tool_id in prepared:
        # Replace this tool's explicit dependency set, preserving all other
        # catalog relationships and never inferring edges from names.
        await db.query("MATCH (t:Tool {id: $id})-[r:DEPENDS_ON]->() DELETE r", {"id": tool_id})
        for prerequisite in tool["prerequisites"]:
            dep_key = _tool_key(catalog, prerequisite)
            dep_id = HydraClient.id_for("Tool", dep_key)
            await _ensure_edge(db, tool_id, "DEPENDS_ON", dep_id)
    return {"catalog": catalog, "catalog_version": version, "fingerprint": fingerprint,
        "registered": [{"id": _tool_key(catalog, tool["name"]), "name": tool["name"],
            "server": tool["server"], "prerequisites": tool["prerequisites"]} for tool in tools]}
