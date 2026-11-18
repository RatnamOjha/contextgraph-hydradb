"""Thin MCP client for ContextGraph's read-only routing and registry APIs."""
from __future__ import annotations

import json
import os
import re
from typing import Any
from urllib.parse import urlparse

from mcp.server.mcpserver import MCPServer
from contextgraph import AsyncContextGraph, ContextGraphError

DEFAULT_API_URL = "https://contextgraph-demo.onrender.com"
ORDER_ID_RE = re.compile(r"^[0-9a-f]{32}$", re.I)
API_BASE = os.getenv("CONTEXTGRAPH_API_URL", DEFAULT_API_URL).rstrip("/")
_parsed_base = urlparse(API_BASE)
if _parsed_base.scheme not in {"http", "https"} or not _parsed_base.netloc:
    raise RuntimeError("CONTEXTGRAPH_API_URL must be an absolute http(s) URL")
if _parsed_base.username or _parsed_base.password or _parsed_base.query or _parsed_base.fragment:
    raise RuntimeError("CONTEXTGRAPH_API_URL must not contain credentials, query parameters, or a fragment")

mcp = MCPServer(
    name="ContextGraph",
    version="0.1.0",
    instructions=("Retrieve dependency-aware tool context from a configured ContextGraph API. "
        "These tools inspect routing and metadata only; they do not execute Olist tools."),
)


async def request_json(path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    try:
        async with AsyncContextGraph(API_BASE, timeout=12.0, retries=1) as graph:
            if path == "/api/graph/explore":
                task = (params or {}).get("task", "")
                result = await graph.retrieve(task, order_id=(params or {}).get("order_id"))
                return _retrieval_payload(result, (params or {}).get("order_id"))
            if path == "/api/tools":
                tools = await graph.list_tools()
                return {"tools": [{"key": tool.id, "name": tool.name,
                    "description": tool.description, "schema": tool.input_schema,
                    "output_schema": tool.output_schema, "server": tool.server,
                    "skill": tool.skill, "execution_status": tool.execution_status,
                    "source": tool.provenance, "dependencies": tool.dependencies,
                    "catalog": tool.model_extra.get("catalog") if tool.model_extra else None}
                    for tool in tools]}
            raise ValueError(f"Unsupported SDK-backed endpoint: {path}")
    except ContextGraphError as exc:
        raise RuntimeError(str(exc)) from exc


def _retrieval_payload(result: Any, order_id: str | None = None) -> dict[str, Any]:
    metadata = result.metadata
    return {"task": result.task, "order_id": order_id,
        "catalog": metadata.catalog, "skills": metadata.skills,
        "nodes": metadata.graph_nodes, "edges": metadata.graph_edges,
        "resolved_entities": metadata.resolved_entities, "retrieval_ms": result.retrieval_ms,
        "bounded": metadata.bounded, "missing_dependencies": result.missing_dependencies,
        "omitted_tool_ids": result.omitted_tool_ids, "complete": result.complete,
        "execution_performed": result.execution_performed}


async def _retrieve_payload(task: str, order_id: str | None, catalog: str | None,
                            max_tools: int, known_tool_ids: list[str] | None,
                            exclude_tool_ids: list[str] | None) -> dict[str, Any]:
    if catalog is None:
        params = {"task": task}
        if order_id:
            params["order_id"] = order_id
        return await request_json("/api/graph/explore", params)
    try:
        async with AsyncContextGraph(API_BASE, timeout=12.0, retries=1) as graph:
            result = await graph.retrieve(task, catalog=catalog, max_tools=max_tools,
                known_tool_ids=known_tool_ids, exclude_tool_ids=exclude_tool_ids)
            return _retrieval_payload(result)
    except ContextGraphError as exc:
        raise RuntimeError(str(exc)) from exc


def _validate_task_and_order(task: str, order_id: str | None) -> None:
    if not 8 <= len(task) <= 1200:
        raise ValueError("task must contain between 8 and 1200 characters")
    if order_id is not None and not ORDER_ID_RE.fullmatch(order_id):
        raise ValueError("order_id must be a 32-character hexadecimal Olist identifier")


def _context_result(graph: dict[str, Any]) -> dict[str, Any]:
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    tools = [
        {"id": node["id"], **(node.get("metadata") or {})}
        for node in nodes if node.get("type") == "Tool"
    ]
    dependency_edges = [edge for edge in edges if edge.get("type") == "DEPENDS_ON"]
    return {
        "task": graph.get("task"),
        "order_id": graph.get("order_id"),
        "selected_tools": tools,
        "required_dependencies": dependency_edges,
        "retrieval_relationships": edges,
        "resolved_entities": graph.get("resolved_entities", []),
        "retrieval_ms": graph.get("retrieval_ms"),
        "provenance": ("Read-only developer catalog route returned by the ContextGraph SDK"
            if graph.get("catalog") else "Read-only Olist demo route returned by the ContextGraph SDK"),
        "execution_performed": False,
        "complete": graph.get("complete", True),
        "missing_dependencies": graph.get("missing_dependencies", []),
        "omitted_tool_ids": graph.get("omitted_tool_ids", []),
    }


@mcp.tool()
async def retrieve_context(task: str, order_id: str | None = None, catalog: str | None = None,
                           max_tools: int = 8, known_tool_ids: list[str] | None = None,
                           exclude_tool_ids: list[str] | None = None) -> dict[str, Any]:
    """Retrieve task-selected tools, schemas, prerequisites, and provenance from ContextGraph.

    This is context retrieval only. It does not execute the returned tools.
    """
    _validate_task_and_order(task, order_id)
    if not 1 <= max_tools <= 50:
        raise ValueError("max_tools must be between 1 and 50")
    if catalog is not None and order_id is not None:
        raise ValueError("order_id is only supported by the Olist demo retrieval path")
    if catalog is None and (known_tool_ids or exclude_tool_ids or max_tools != 8):
        raise ValueError("catalog is required for generic retrieval options")
    return _context_result(await _retrieve_payload(task, order_id, catalog, max_tools,
        known_tool_ids, exclude_tool_ids))


@mcp.tool()
async def inspect_tool(tool_id: str) -> dict[str, Any]:
    """Inspect a registered tool's schema, provenance, prerequisites, and related tools."""
    if not tool_id or len(tool_id) > 120:
        raise ValueError("tool_id must contain 1–120 characters")
    result = await request_json("/api/tools")
    tools = result.get("tools", [])
    current = next((tool for tool in tools if tool.get("key") == tool_id), None)
    if current is None:
        raise ValueError(f"Unknown ContextGraph tool: {tool_id}")
    incoming = sorted(tool["key"] for tool in tools if tool_id in (tool.get("dependencies") or []))
    schema: Any = current.get("schema")
    if isinstance(schema, str):
        try:
            schema = json.loads(schema)
        except ValueError:
            pass
    return {
        "tool": current.get("key"),
        "name": current.get("name"),
        "description": current.get("description"),
        "input_schema": schema,
        "output_metadata": None,
        "logical_server": current.get("server"),
        "execution_provenance": current.get("source"),
        "execution_status": current.get("execution_status"),
        "prerequisites": current.get("dependencies", []),
        "incoming_dependencies": incoming,
        "related_skill": current.get("skill"),
        "provenance_note": "Registered logical tool metadata; no external tool server is invoked.",
    }


@mcp.tool()
async def explain_context(task: str, order_id: str | None = None, catalog: str | None = None,
                         max_tools: int = 8, known_tool_ids: list[str] | None = None,
                         exclude_tool_ids: list[str] | None = None) -> dict[str, Any]:
    """Explain why tools were selected and which actual graph relationships expanded context."""
    _validate_task_and_order(task, order_id)
    if not 1 <= max_tools <= 50:
        raise ValueError("max_tools must be between 1 and 50")
    if catalog is None and (known_tool_ids or exclude_tool_ids or max_tools != 8):
        raise ValueError("catalog is required for generic retrieval options")
    graph = await _retrieve_payload(task, order_id, catalog, max_tools, known_tool_ids, exclude_tool_ids)
    tools = [node for node in graph.get("nodes", []) if node.get("type") == "Tool"]
    return {
        "task": task,
        "order_id": order_id,
        "candidate_skills": graph.get("skills", []),
        "selected_tools": [node.get("id") for node in tools],
        "selection_reasons": {node.get("id"): (node.get("metadata") or {}).get("why", []) for node in tools},
        "dependency_expansions": [edge for edge in graph.get("edges", []) if edge.get("type") == "DEPENDS_ON"],
        "other_relationships": [edge for edge in graph.get("edges", []) if edge.get("type") != "DEPENDS_ON"],
        "resolved_entities": graph.get("resolved_entities", []),
        "retrieval_ms": graph.get("retrieval_ms"),
        "saved_routing_id": None,
        "complete": graph.get("complete", True),
        "missing_dependencies": graph.get("missing_dependencies", []),
        "omitted_tool_ids": graph.get("omitted_tool_ids", []),
        "note": "This API does not expose saved routing IDs; the explanation is for the current read-only route.",
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
