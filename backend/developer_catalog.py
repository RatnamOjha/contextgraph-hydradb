"""Safe, scoped registration primitives for developer-owned tool catalogs."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .hydra import HydraClient

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")


def catalog_fingerprint(tools: list[dict[str, Any]]) -> str:
    canonical_tools = []
    for tool in tools:
        canonical_tools.append({
            "id": tool.get("id", tool.get("key", "")),
            "name": tool.get("name", ""),
            "description": tool.get("description") or "",
            "input_schema": _decode_json(tool.get("input_schema") if tool.get("input_schema") is not None
                else tool.get("schema")),
            "output_schema": _decode_json(tool.get("output_schema")),
            "server": tool.get("server") or "",
            "provenance": tool.get("provenance") or "developer_registered",
            "prerequisites": sorted(set(tool.get("prerequisites") or [])),
        })
    canonical_tools.sort(key=lambda item: item["id"] or item["name"])
    canonical = json.dumps(canonical_tools, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _decode_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


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
        prerequisites = raw.get("prerequisites") if "prerequisites" in raw else None
        if prerequisites is not None and (not isinstance(prerequisites, list)
                or any(not isinstance(dep, str) for dep in prerequisites)):
            raise ValueError(f"prerequisites for {name} must be a list of tool names")
        if prerequisites is not None and (len(set(prerequisites)) != len(prerequisites) or name in prerequisites):
            raise ValueError(f"prerequisites for {name} contain duplicates or a self-reference")
        if prerequisites is not None and any(not _IDENTIFIER.fullmatch(dep) for dep in prerequisites):
            raise ValueError(f"prerequisites for {name} must be tool names in the same catalog")
        normalized.append({"name": name, "description": description, "input_schema": input_schema,
            "output_schema": output_schema, "server": server, "prerequisites": prerequisites})
    # Do not create dangling dependency edges: every prerequisite must exist in
    # this registration batch or already be present in this same catalog.
    names = {tool["name"] for tool in normalized} | (existing_tool_names or set())
    for tool in normalized:
        unresolved = [dep for dep in (tool["prerequisites"] or []) if dep not in names]
        if unresolved:
            raise ValueError(f"unknown prerequisites for {tool['name']}: {', '.join(unresolved)}")
    return sorted(normalized, key=lambda tool: tool["name"])


async def register_catalog_tools(db: HydraClient, catalog: str, raw_tools: list[dict[str, Any]]) -> dict[str, Any]:
    _, existing_rows, _ = await db.query(
        "MATCH (t:Tool {catalog: $catalog}) RETURN t.key AS key,t.name AS name,t.description AS description,"
        "t.input_schema AS input_schema,t.schema AS schema,t.output_schema AS output_schema,t.server AS server,"
        "t.provenance AS provenance ORDER BY t.key LIMIT 201", {"catalog": catalog})
    if len(existing_rows) > 200:
        raise ValueError("catalog exceeds the 200-tool registration bound")
    existing = [dict(zip(["key", "name", "description", "input_schema", "schema", "output_schema", "server", "provenance"], row))
        for row in existing_rows]
    existing_by_key = {str(row["key"]): row for row in existing if row.get("key") is not None}
    existing_by_name = {str(row.get("name") or str(key).split(".", 1)[-1]): row
        for key, row in existing_by_key.items()}
    existing_names = set(existing_by_name)
    tools = validate_catalog_registration(catalog, raw_tools, existing_names)
    raw_by_name = {raw["name"]: raw for raw in raw_tools}
    for tool in tools:
        raw = raw_by_name[tool["name"]]
        previous = existing_by_name.get(tool["name"])
        if previous is None:
            continue
        if "description" not in raw:
            tool["description"] = previous.get("description") or ""
        if "input_schema" not in raw:
            stored_schema = previous.get("input_schema")
            tool["input_schema"] = _decode_json(stored_schema if stored_schema is not None
                else previous.get("schema"))
        if "output_schema" not in raw:
            tool["output_schema"] = _decode_json(previous.get("output_schema"))
        if "server" not in raw:
            stored_server = str(previous.get("server") or catalog)
            tool["server"] = stored_server[len(catalog) + 1:] if stored_server.startswith(catalog + ".") else stored_server
    catalog_id = HydraClient.id_for("Catalog", catalog)
    await db.query("UNWIND $rows AS row MERGE (c {id: row.id}) SET c:Catalog, c.key=row.catalog, c.name=row.catalog",
        {"rows": [{"id": catalog_id, "catalog": catalog}]})
    prepared: list[tuple[dict[str, Any], str, int]] = []
    for tool in tools:
        key = _tool_key(catalog, tool["name"])
        tool_id = HydraClient.id_for("Tool", key)
        server_key = f"{catalog}.{tool['server']}"
        server_id = HydraClient.id_for("MCPServer", server_key)
        encoded_input = json.dumps(tool["input_schema"], sort_keys=True, separators=(",", ":")) if tool["input_schema"] is not None else None
        encoded_output = json.dumps(tool["output_schema"], sort_keys=True, separators=(",", ":")) if tool["output_schema"] is not None else None
        assignments = ["t:Tool", "t.key=row.key", "t.name=row.name", "t.description=row.description",
            "t.server=row.server", "t.catalog=row.catalog", "t.provenance=row.provenance"]
        row = {"tool_id": tool_id, "key": key, "name": tool["name"], "description": tool["description"],
            "server": server_key, "catalog": catalog,
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
        # Only an explicitly supplied list changes edges. Missing prerequisites
        # preserve existing edges on refresh; new tools have none by default.
        if tool["prerequisites"] is None:
            continue
        await db.query("MATCH (t:Tool {id: $id})-[r:DEPENDS_ON]->() DELETE r", {"id": tool_id})
        for prerequisite in tool["prerequisites"]:
            dep_key = _tool_key(catalog, prerequisite)
            dep_id = HydraClient.id_for("Tool", dep_key)
            await _ensure_edge(db, tool_id, "DEPENDS_ON", dep_id)

    # Finalize the fingerprint from the *effective graph state*, including
    # untouched tools and dependency edges that were preserved on refresh.
    _, effective_rows, _ = await db.query(
        "MATCH (t:Tool {catalog: $catalog}) RETURN t.key AS key,t.name AS name,t.description AS description,"
        "t.input_schema AS input_schema,t.schema AS schema,t.output_schema AS output_schema,t.server AS server,"
        "t.provenance AS provenance ORDER BY t.key LIMIT 201", {"catalog": catalog})
    if len(effective_rows) > 200:
        raise ValueError("catalog exceeds the 200-tool registration bound")
    effective = [dict(zip(["key", "name", "description", "input_schema", "schema", "output_schema", "server", "provenance"], row))
        for row in effective_rows]
    _, effective_dependency_rows, _ = await db.query(
        "MATCH (a:Tool {catalog: $catalog})-[:DEPENDS_ON]->(b:Tool) "
        "RETURN a.key AS source,b.key AS target LIMIT 20001", {"catalog": catalog})
    if len(effective_dependency_rows) > 20000:
        raise ValueError("catalog exceeds the 20000-edge fingerprint bound")
    effective_dependencies: dict[str, list[str]] = {}
    for source, target in effective_dependency_rows:
        if source and target:
            effective_dependencies.setdefault(str(source), []).append(str(target))
    for row in effective:
        row["prerequisites"] = sorted(set(effective_dependencies.get(str(row.get("key")), [])))
    fingerprint = catalog_fingerprint(effective)
    version = fingerprint[:16]
    await db.query("UNWIND $rows AS row MERGE (c {id: row.id}) SET c:Catalog, c.key=row.catalog, c.name=row.catalog, c.fingerprint=row.fingerprint, c.version=row.version",
        {"rows": [{"id": catalog_id, "catalog": catalog, "fingerprint": fingerprint, "version": version}]})
    await db.query("MATCH (t:Tool {catalog: $catalog}) SET t.catalog_version=$version,t.catalog_fingerprint=$fingerprint",
        {"catalog": catalog, "version": version, "fingerprint": fingerprint})
    return {"catalog": catalog, "catalog_version": version, "fingerprint": fingerprint,
        "registered": [{"id": _tool_key(catalog, tool["name"]), "name": tool["name"],
            "server": tool["server"], "prerequisites": sorted(set(effective_dependencies.get(_tool_key(catalog, tool["name"]), [])))}
            for tool in tools]}
