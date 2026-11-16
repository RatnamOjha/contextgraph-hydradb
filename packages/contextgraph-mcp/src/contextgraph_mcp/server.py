"""Thin MCP client for ContextGraph's read-only routing and registry APIs."""
from __future__ import annotations

import json
import os
import re
from typing import Any
from urllib.parse import urlparse

import httpx
from mcp.server.mcpserver import MCPServer

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
    url = f"{API_BASE}{path}"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=5.0), follow_redirects=False) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        detail = ""
        try:
            body = exc.response.json()
            detail = str(body.get("detail", ""))[:300]
        except (ValueError, AttributeError):
            detail = exc.response.text[:300]
        raise RuntimeError(f"ContextGraph API returned HTTP {exc.response.status_code}: {detail or 'request rejected'}") from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeError(f"Could not read ContextGraph API at {API_BASE}: {type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("ContextGraph API returned an unexpected JSON shape")
    return data


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
        "provenance": "Read-only HydraDB route returned by ContextGraph /api/graph/explore",
        "execution_performed": False,
    }


@mcp.tool()
async def retrieve_context(task: str, order_id: str | None = None) -> dict[str, Any]:
    """Retrieve task-selected tools, schemas, prerequisites, and provenance from ContextGraph.

    This is context retrieval only. It does not execute the returned tools.
    """
    _validate_task_and_order(task, order_id)
    params = {"task": task}
    if order_id:
        params["order_id"] = order_id
    return _context_result(await request_json("/api/graph/explore", params))


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
async def explain_context(task: str, order_id: str | None = None) -> dict[str, Any]:
    """Explain why tools were selected and which actual graph relationships expanded context."""
    _validate_task_and_order(task, order_id)
    params = {"task": task}
    if order_id:
        params["order_id"] = order_id
    graph = await request_json("/api/graph/explore", params)
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
        "note": "This API does not expose saved routing IDs; the explanation is for the current read-only route.",
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
