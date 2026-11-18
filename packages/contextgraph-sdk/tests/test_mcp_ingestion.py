import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

from contextgraph import AsyncContextGraph, ContextGraph
from contextgraph.mcp_ingestion import _validate_schema, discover_stdio_tools

FIXTURE = Path(__file__).parent / "fixtures" / "unfamiliar_mcp_server.py"


def test_stdio_server_discovery_uses_official_protocol_and_registers_only_metadata():
    captured = []
    def handler(request):
        captured.append(request)
        payload = json.loads(request.content)
        assert request.method == "POST"
        assert len(payload["tools"]) == 5
        return httpx.Response(200, json={
            "catalog": "ops", "catalog_version": "fixture-version", "fingerprint": "fixture-hash",
            "registered": [{"id": f"ops.{tool['name']}"} for tool in payload["tools"]],
        })
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with ContextGraph("http://contextgraph.local", retries=0, http_client=client) as cg:
        report = cg.add_mcp_server("factory", sys.executable, [str(FIXTURE)], catalog="ops", timeout=15)
    payload = json.loads(captured[0].content)
    names = {tool["name"] for tool in payload["tools"]}
    assert names == {"factory.get_order", "factory.check_refund", "factory.list_replacement_parts",
        "factory.check_part_stock", "factory.schedule_technician"}
    assert all(tool["prerequisites"] == [] for tool in payload["tools"])
    assert all(tool["input_schema"] for tool in payload["tools"])
    assert report.server == "factory"
    assert report.catalog == "ops"
    assert report.discovered_tool_count == 5
    assert report.catalog_version == "fixture-version"


def test_async_stdio_server_discovery():
    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"registered": [], "catalog": "factory"})))
        async with AsyncContextGraph("http://contextgraph.local", retries=0, http_client=client) as cg:
            report = await cg.add_mcp_server("factory", sys.executable, [str(FIXTURE)], timeout=15)
        assert report.discovered_tool_count == 0
        assert report.server == "factory"
    asyncio.run(run())


def test_same_tool_name_in_different_servers_gets_distinct_stable_identity():
    async def run():
        command_args = [str(FIXTURE)]
        first = await discover_stdio_tools(server_name="factory", command=sys.executable,
            args=command_args, env=None, cwd=None, timeout=15)
        second = await discover_stdio_tools(server_name="warehouse", command=sys.executable,
            args=command_args, env=None, cwd=None, timeout=15)
        first_stock = next(tool["name"] for tool in first if tool["name"].endswith("check_part_stock"))
        second_stock = next(tool["name"] for tool in second if tool["name"].endswith("check_part_stock"))
        assert first_stock == "factory.check_part_stock"
        assert second_stock == "warehouse.check_part_stock"
    asyncio.run(run())


def test_invalid_schema_is_rejected_and_absent_schema_stays_absent():
    assert _validate_schema(None, "no_schema", "input schema") is None
    with pytest.raises(ValueError, match="invalid input schema"):
        _validate_schema({"type": "not-a-json-schema-type"}, "bad_tool", "input schema")

