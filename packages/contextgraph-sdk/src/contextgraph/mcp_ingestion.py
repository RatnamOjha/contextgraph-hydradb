"""Discover tool definitions from explicitly configured local MCP stdio servers."""
from __future__ import annotations

import asyncio
import re
from typing import Any

from .exceptions import ContextGraphError
from .models import ContextGraphErrorInfo, MCPIngestionReport

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")


async def discover_stdio_tools(
    *, server_name: str, command: str, args: list[str] | None, env: dict[str, str] | None,
    cwd: str | None, timeout: float,
) -> list[dict[str, Any]]:
    """Use the official MCP SDK to initialize a session and list tools only."""
    if not _NAME.fullmatch(server_name):
        raise ValueError("server name must be a stable 1–80 character identifier")
    if not command or not isinstance(command, str):
        raise ValueError("command must be an explicit executable name or path")
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.types import PaginatedRequestParams
    except ImportError as exc:
        raise RuntimeError("MCP discovery requires the contextgraph-sdk MCP dependency") from exc

    server = StdioServerParameters(command=command, args=args or [], env=env, cwd=cwd)
    try:
        async with asyncio.timeout(timeout):
            async with stdio_client(server) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    tools: list[dict[str, Any]] = []
                    cursor: str | None = None
                    seen: set[str] = set()
                    for _ in range(20):
                        response = await session.list_tools(params=PaginatedRequestParams(cursor=cursor) if cursor else None)
                        for item in response.tools:
                            if item.name in seen:
                                raise ValueError(f"MCP server {server_name!r} returned a duplicate tool name")
                            seen.add(item.name)
                            if not isinstance(item.name, str) or not item.name:
                                raise ValueError("MCP server returned a tool with an empty name")
                            # Preserve JSON Schemas exactly when supplied. MCP
                            # tools do not provide prerequisite declarations in
                            # the standard tool definition, so none are inferred.
                            raw_input_schema = getattr(item, "inputSchema", getattr(item, "input_schema", None))
                            raw_output_schema = getattr(item, "outputSchema", getattr(item, "output_schema", None))
                            input_schema = _validate_schema(raw_input_schema, item.name, "input schema")
                            output_schema = _validate_schema(raw_output_schema, item.name, "output schema")
                            description = item.description or ""
                            if len(description) > 4000:
                                raise ValueError(f"description for {item.name} exceeds 4000 characters")
                            tools.append({
                                "name": f"{server_name}.{item.name}",
                                "description": description,
                                "input_schema": input_schema,
                                "output_schema": output_schema,
                                "server": server_name,
                                "prerequisites": [],
                            })
                            if len(tools) > 200:
                                raise ValueError("MCP server exposes more than 200 tools; split it across catalogs")
                        cursor = getattr(response, "nextCursor", getattr(response, "next_cursor", None))
                        if not cursor:
                            break
                    else:
                        raise ValueError("MCP server exceeded the 20-page tool discovery limit")
                    if not tools:
                        raise ValueError(f"MCP server {server_name!r} exposes no tools")
                    invalid = [tool["name"] for tool in tools if not _NAME.fullmatch(tool["name"])]
                    if invalid:
                        raise ValueError(f"MCP tool names cannot be represented safely: {', '.join(invalid[:5])}")
                    return tools
    except ContextGraphError:
        raise
    except TimeoutError as exc:
        raise ContextGraphError(ContextGraphErrorInfo(
            message=f"MCP server discovery timed out after {timeout:g} seconds",
            method="MCP stdio", retryable=False)) from exc
    except Exception as exc:
        if isinstance(exc, (ValueError, RuntimeError)):
            raise
        # Server startup exceptions may include local command arguments or
        # environment details. Report type only to avoid leaking them.
        raise ContextGraphError(ContextGraphErrorInfo(
            message=f"MCP server discovery failed ({type(exc).__name__})",
            method="MCP stdio", retryable=False)) from exc


def ingestion_report(server_name: str, catalog: str, response: dict[str, Any]) -> MCPIngestionReport:
    registered = response.get("registered") or []
    return MCPIngestionReport(
        server=server_name,
        catalog=catalog,
        discovered_tool_count=len(registered),
        registered_tool_ids=[str(tool.get("id")) for tool in registered if isinstance(tool, dict) and tool.get("id")],
        catalog_version=response.get("catalog_version"),
        fingerprint=response.get("fingerprint"),
    )


def _validate_schema(schema: Any, tool_name: str, schema_name: str) -> dict[str, Any] | None:
    if schema is None:
        return None
    if not isinstance(schema, dict):
        raise ValueError(f"{schema_name} for {tool_name} must be a JSON object")
    try:
        from jsonschema import Draft202012Validator
        Draft202012Validator.check_schema(schema)
    except ImportError:
        pass
    except Exception as exc:
        raise ValueError(f"invalid {schema_name} for {tool_name} ({type(exc).__name__})") from exc
    return schema
