"""Typed sync and async clients for ContextGraph's read-only retrieval API."""
from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import quote

import httpx

from .exceptions import ContextGraphError
from .evaluation import evaluate_results, validate_task_specs
from .mcp_ingestion import discover_stdio_tools, ingestion_report
from .models import (
    HealthResponse,
    MCPIngestionReport,
    RetrievalExplanation,
    RetrievalMetadata,
    RetrievalResult,
    ToolDefinition,
    ToolDependency,
)
from .transport import AsyncTransport, SyncTransport


def _schema(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, json.JSONDecodeError):
            return value
    return value


def _normalize_tool(row: dict[str, Any]) -> ToolDefinition:
    return ToolDefinition(
        id=str(row.get("id") or row.get("key") or ""),
        name=str(row.get("name") or row.get("key") or ""),
        description=str(row.get("description") or ""),
        input_schema=_schema(row.get("input_schema", row.get("schema"))),
        output_schema=_schema(row.get("output_schema")),
        server=row.get("server") or row.get("mcp_server"),
        skill=row.get("skill"),
        dependencies=row.get("dependencies") or [],
        provenance=row.get("provenance") or row.get("source") or row.get("execution_provenance"),
        execution_status=row.get("execution_status"),
        catalog=row.get("catalog"),
    )


def _tool_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("tools")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ContextGraphError.from_response("ContextGraph API returned an invalid tools list", "/api/tools")
    return rows


def _normalize_retrieval(payload: dict[str, Any], task: str, order_id: str | None,
                         endpoint: str = "/api/graph/explore") -> RetrievalResult:
    nodes = payload.get("nodes", [])
    edges = payload.get("edges", [])
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise ContextGraphError.from_response("ContextGraph API returned an invalid graph shape", "/api/graph/explore")
    node_map = {str(n.get("id")): n for n in nodes if isinstance(n, dict) and n.get("id") is not None}
    tool_nodes = [n for n in nodes if isinstance(n, dict) and n.get("type") == "Tool"]
    tools = []
    reasons: dict[str, list[str]] = {}
    dependencies: list[ToolDependency] = []
    for node in tool_nodes:
        meta = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
        key = str(node.get("id") or node.get("label") or "")
        tools.append(_normalize_tool({**meta, "key": key, "name": meta.get("name") or node.get("label")}))
        why = meta.get("why") or []
        reasons[key] = [str(item) for item in why] if isinstance(why, list) else [str(why)]
        deps = meta.get("dependencies") or []
        if isinstance(deps, list):
            dependencies.extend(ToolDependency(source_tool_id=key, required_tool_id=str(dep)) for dep in deps)

    # Keep declared prerequisite metadata (including missing edges) alongside
    # the actual graph relationships returned by the backend.
    edge_dependencies: list[ToolDependency] = list(dependencies)
    for edge in edges:
        if not isinstance(edge, dict) or edge.get("type") != "DEPENDS_ON":
            continue
        source, target = edge.get("from"), edge.get("to")
        if source is not None and target is not None:
            edge_dependencies.append(ToolDependency(source_tool_id=str(source), required_tool_id=str(target)))
    unique_dependencies = {(dep.source_tool_id, dep.required_tool_id): dep for dep in edge_dependencies}
    dependencies = list(unique_dependencies.values())

    selected = {tool.id for tool in tools}
    known = set(payload.get("known_tool_ids") or [])
    missing = sorted(set(payload.get("missing_dependencies") or []) | {
        dep.required_tool_id for dep in dependencies if dep.source_tool_id in selected
        and dep.required_tool_id not in selected and dep.required_tool_id not in known
    })
    resolved = payload.get("resolved_entities") or []
    skills = payload.get("skills") or []
    source_values = {str((node.get("metadata") or {}).get("execution_provenance", "")) for node in tool_nodes}
    source_values.discard("")
    source = ", ".join(sorted(source_values)) or "ContextGraph graph retrieval"
    return RetrievalResult(
        task=str(payload.get("task") or task), tools=tools, dependencies=dependencies,
        selection_reasons=reasons, missing_dependencies=missing,
        omitted_tool_ids=payload.get("omitted_tool_ids") or [],
        known_tool_ids=sorted(known),
        retrieval_ms=payload.get("retrieval_ms"), source=source,
        metadata=RetrievalMetadata(skills=skills, resolved_entities=resolved,
            bounded=payload.get("bounded") or {}, endpoint=endpoint, catalog=payload.get("catalog"),
            graph_nodes=[node for node in nodes if isinstance(node, dict)],
            graph_edges=[edge for edge in edges if isinstance(edge, dict)]),
        complete=bool(payload.get("complete", not missing)),
    )


def _explanation(result: RetrievalResult) -> RetrievalExplanation:
    return RetrievalExplanation(task=result.task, selected_tool_ids=[tool.id for tool in result.tools],
        selection_reasons=result.selection_reasons, dependencies=result.dependencies,
        missing_dependencies=result.missing_dependencies, known_tool_ids=result.known_tool_ids,
        omitted_tool_ids=result.omitted_tool_ids,
        complete=result.complete, source=result.source,
        retrieval_ms=result.retrieval_ms)


class ContextGraph:
    """Synchronous client. Retrieval is read-only and never executes tools."""

    def __init__(self, url: str, *, timeout: float = 15.0, api_key: str | None = None,
                 retries: int = 1, http_client: httpx.Client | None = None) -> None:
        self._transport = SyncTransport(url, timeout, retries, api_key, http_client=http_client)

    def close(self) -> None:
        self._transport.close()

    def __enter__(self) -> "ContextGraph":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def health(self) -> HealthResponse:
        return HealthResponse.model_validate(self._transport.get("/api/health"))

    def list_tools(self) -> list[ToolDefinition]:
        return [_normalize_tool(row) for row in _tool_rows(self._transport.get("/api/tools"))]

    def inspect_tool(self, tool_id: str) -> ToolDefinition:
        for tool in self.list_tools():
            if tool.id == tool_id:
                return tool
        raise KeyError(f"No tool with id {tool_id!r} is present in the catalog")

    def register_tools(self, catalog: str, tools: list[dict[str, Any]]) -> dict[str, Any]:
        """Register explicit tool definitions in a trusted local catalog."""
        path = f"/api/v1/catalogs/{quote(catalog, safe='')}/tools"
        return self._transport.post(path, {"tools": tools})

    def add_mcp_server(self, name: str, command: str, args: list[str] | None = None, *,
                       catalog: str | None = None, env: dict[str, str] | None = None,
                       cwd: str | None = None, timeout: float = 20.0) -> MCPIngestionReport:
        """Discover tools from an explicitly trusted stdio MCP command and register metadata.

        The configured process is launched, but none of its business tools are called.
        """
        actual_catalog = catalog or "default"
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("add_mcp_server() cannot run inside an active event loop; use AsyncContextGraph")
        tools = asyncio.run(discover_stdio_tools(server_name=name, command=command, args=args,
            env=env, cwd=cwd, timeout=timeout))
        response = self.register_tools(actual_catalog, tools)
        return ingestion_report(name, actual_catalog, response)

    def retrieve(self, task: str, *, order_id: str | None = None, catalog: str | None = None,
                 max_tools: int = 8, known_tool_ids: list[str] | None = None,
                 exclude_tool_ids: list[str] | None = None) -> RetrievalResult:
        if not 8 <= len(task) <= 1200:
            raise ValueError("task must contain 8–1200 characters")
        if catalog is not None:
            if order_id is not None:
                raise ValueError("order_id is only supported by the Olist demo retrieval path")
            endpoint = f"/api/v1/catalogs/{quote(catalog, safe='')}/retrieve"
            payload = self._transport.post(endpoint, {"task": task, "max_tools": max_tools,
                "known_tool_ids": known_tool_ids or [], "exclude_tool_ids": exclude_tool_ids or []})
            return _normalize_retrieval(payload, task, None, endpoint)
        if known_tool_ids or exclude_tool_ids or max_tools != 8:
            raise ValueError("catalog is required for max_tools, known_tool_ids, and exclude_tool_ids")
        params = {"task": task}
        if order_id is not None:
            params["order_id"] = order_id
        payload = self._transport.get("/api/graph/explore", params)
        return _normalize_retrieval(payload, task, order_id)

    def explain(self, task: str, *, order_id: str | None = None, catalog: str | None = None,
                max_tools: int = 8, known_tool_ids: list[str] | None = None,
                exclude_tool_ids: list[str] | None = None) -> RetrievalExplanation:
        return _explanation(self.retrieve(task, order_id=order_id, catalog=catalog, max_tools=max_tools,
            known_tool_ids=known_tool_ids, exclude_tool_ids=exclude_tool_ids))

    def evaluate(self, catalog: str, tasks: list[dict[str, Any]], *, max_tools: int = 8):
        """Compare graph retrieval with a local flat lexical baseline on labeled tasks."""
        specs = validate_task_specs(tasks)
        if not 1 <= max_tools <= 50:
            raise ValueError("max_tools must be between 1 and 50")
        tools = [tool for tool in self.list_tools() if tool.catalog == catalog]
        if not tools:
            raise ValueError(f"No tools found in catalog {catalog!r}")
        for spec in specs:
            unknown = set(spec["required_tools"]) - {tool.id for tool in tools}
            if unknown:
                raise ValueError(f"required tools are not in catalog {catalog!r}: {', '.join(sorted(unknown))}")
        graph_results = [self.retrieve(spec["task"], catalog=catalog, max_tools=max_tools) for spec in specs]
        return evaluate_results(catalog, specs, tools, graph_results, max_tools)


class AsyncContextGraph:
    """Asynchronous client with the same response models as :class:`ContextGraph`."""

    def __init__(self, url: str, *, timeout: float = 15.0, api_key: str | None = None,
                 retries: int = 1, http_client: httpx.AsyncClient | None = None) -> None:
        self._transport = AsyncTransport(url, timeout, retries, api_key, http_client=http_client)

    async def close(self) -> None:
        await self._transport.close()

    async def __aenter__(self) -> "AsyncContextGraph":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def health(self) -> HealthResponse:
        return HealthResponse.model_validate(await self._transport.get("/api/health"))

    async def list_tools(self) -> list[ToolDefinition]:
        return [_normalize_tool(row) for row in _tool_rows(await self._transport.get("/api/tools"))]

    async def inspect_tool(self, tool_id: str) -> ToolDefinition:
        for tool in await self.list_tools():
            if tool.id == tool_id:
                return tool
        raise KeyError(f"No tool with id {tool_id!r} is present in the catalog")

    async def register_tools(self, catalog: str, tools: list[dict[str, Any]]) -> dict[str, Any]:
        """Register explicit tool definitions in a trusted local catalog."""
        path = f"/api/v1/catalogs/{quote(catalog, safe='')}/tools"
        return await self._transport.post(path, {"tools": tools})

    async def add_mcp_server(self, name: str, command: str, args: list[str] | None = None, *,
                             catalog: str | None = None, env: dict[str, str] | None = None,
                             cwd: str | None = None, timeout: float = 20.0) -> MCPIngestionReport:
        """Async equivalent of MCP stdio discovery and local catalog registration."""
        actual_catalog = catalog or "default"
        tools = await discover_stdio_tools(server_name=name, command=command, args=args,
            env=env, cwd=cwd, timeout=timeout)
        response = await self.register_tools(actual_catalog, tools)
        return ingestion_report(name, actual_catalog, response)

    async def retrieve(self, task: str, *, order_id: str | None = None, catalog: str | None = None,
                       max_tools: int = 8, known_tool_ids: list[str] | None = None,
                       exclude_tool_ids: list[str] | None = None) -> RetrievalResult:
        if not 8 <= len(task) <= 1200:
            raise ValueError("task must contain 8–1200 characters")
        if catalog is not None:
            if order_id is not None:
                raise ValueError("order_id is only supported by the Olist demo retrieval path")
            endpoint = f"/api/v1/catalogs/{quote(catalog, safe='')}/retrieve"
            payload = await self._transport.post(endpoint, {"task": task, "max_tools": max_tools,
                "known_tool_ids": known_tool_ids or [], "exclude_tool_ids": exclude_tool_ids or []})
            return _normalize_retrieval(payload, task, None, endpoint)
        if known_tool_ids or exclude_tool_ids or max_tools != 8:
            raise ValueError("catalog is required for max_tools, known_tool_ids, and exclude_tool_ids")
        params = {"task": task}
        if order_id is not None:
            params["order_id"] = order_id
        payload = await self._transport.get("/api/graph/explore", params)
        return _normalize_retrieval(payload, task, order_id)

    async def explain(self, task: str, *, order_id: str | None = None, catalog: str | None = None,
                      max_tools: int = 8, known_tool_ids: list[str] | None = None,
                      exclude_tool_ids: list[str] | None = None) -> RetrievalExplanation:
        return _explanation(await self.retrieve(task, order_id=order_id, catalog=catalog, max_tools=max_tools,
            known_tool_ids=known_tool_ids, exclude_tool_ids=exclude_tool_ids))

    async def evaluate(self, catalog: str, tasks: list[dict[str, Any]], *, max_tools: int = 8):
        """Async comparison of graph retrieval and a flat lexical baseline."""
        specs = validate_task_specs(tasks)
        if not 1 <= max_tools <= 50:
            raise ValueError("max_tools must be between 1 and 50")
        tools = [tool for tool in await self.list_tools() if tool.catalog == catalog]
        if not tools:
            raise ValueError(f"No tools found in catalog {catalog!r}")
        for spec in specs:
            unknown = set(spec["required_tools"]) - {tool.id for tool in tools}
            if unknown:
                raise ValueError(f"required tools are not in catalog {catalog!r}: {', '.join(sorted(unknown))}")
        graph_results = [await self.retrieve(spec["task"], catalog=catalog, max_tools=max_tools) for spec in specs]
        return evaluate_results(catalog, specs, tools, graph_results, max_tools)
