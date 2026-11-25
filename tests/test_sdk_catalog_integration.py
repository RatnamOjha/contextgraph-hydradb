"""End-to-end registration/retrieval against a disposable local HydraDB graph."""
import json
import asyncio
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from contextgraph import ContextGraph
from scripts.graph_learning_experiment import NODE, HYDRA_SOURCE, isolated_env, wait_ready

ROOT = Path(__file__).resolve().parents[1]
MCP_FIXTURE = ROOT / "packages/contextgraph-sdk/tests/fixtures/unfamiliar_mcp_server.py"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_api(url: str, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("isolated ContextGraph API exited during startup")
        try:
            with urllib.request.urlopen(url, timeout=.5) as response:
                json.load(response)
                return
        except Exception:
            time.sleep(.2)
    raise TimeoutError("isolated ContextGraph API did not become ready")


def _read_catalog_record(http_port: int, token: str, catalog: str = "ops") -> tuple[str, str]:
    from backend.hydra import decode_cell

    request = urllib.request.Request(
        f"http://127.0.0.1:{http_port}/v1/graphs/default/query",
        data=json.dumps({"cell_id": "cell-0",
            "query": "MATCH (c:Catalog {key: $catalog}) RETURN c.version AS version,c.fingerprint AS fingerprint",
            "parameters": {"catalog": catalog}}).encode(),
        headers={"Authorization": f"Bearer {token}", "X-Graph-Namespace": "contextgraph",
            "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=5) as response:
        rows = json.load(response).get("rows", [])
    if len(rows) != 1:
        raise AssertionError(f"expected one persisted Catalog node, got {len(rows)}")
    version, fingerprint = (decode_cell(value) for value in rows[0])
    return str(version), str(fingerprint)


def _hydra_query(http_port: int, token: str, query: str, parameters: dict | None = None):
    from backend.hydra import decode_cell
    body = {"cell_id": "cell-0", "query": query}
    if parameters:
        body["parameters"] = parameters
    request = urllib.request.Request(
        f"http://127.0.0.1:{http_port}/v1/graphs/default/query",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "X-Graph-Namespace": "contextgraph",
            "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=5) as response:
        payload = json.load(response)
    if "error" in payload:
        raise AssertionError(payload["error"])
    return [[decode_cell(value) for value in row] for row in payload.get("rows", [])]


@pytest.mark.skipif(not NODE.is_file(), reason="build local hydradb-upstream graph-node to run isolated integration")
def test_new_mcp_catalog_retrieves_only_task_context_and_expands_prerequisites():
    with tempfile.TemporaryDirectory(prefix="contextgraph-sdk-e2e-") as temp:
        state = Path(temp)
        (state / "store").mkdir()
        (state / "cache").mkdir()
        token_file = state / "auth-token"
        token = secrets.token_urlsafe(40)
        token_file.write_text(token)
        token_file.chmod(0o600)
        hydra_env, ports = isolated_env(state, token_file)
        with (state / "hydra.log").open("wb") as hydra_log:
            hydra = subprocess.Popen([str(NODE)], cwd=HYDRA_SOURCE, env=hydra_env,
                stdout=hydra_log, stderr=subprocess.STDOUT)
            api = None
            try:
                wait_ready(hydra, ports["admin"], state / "hydra.log")
                api_port = _free_port()
                env = os.environ.copy()
                env.update({
                    "HYDRA_URL": f"http://127.0.0.1:{ports['http']}", "HYDRA_GRAPH": "default",
                    "HYDRA_NAMESPACE": "contextgraph", "HYDRA_CELL": "cell-0", "HYDRA_TOKEN": token,
                    "CONTEXTGRAPH_ENV": "development", "CONTEXTGRAPH_ENABLE_LOCAL_REGISTRATION": "1",
                    "PYTHONPATH": str(ROOT) + os.pathsep + env.get("PYTHONPATH", ""),
                })
                with (state / "api.log").open("wb") as api_log:
                    api = subprocess.Popen([sys.executable, "-m", "uvicorn", "backend.app:app",
                        "--host", "127.0.0.1", "--port", str(api_port)], cwd=ROOT, env=env,
                        stdout=api_log, stderr=subprocess.STDOUT)
                    _wait_api(f"http://127.0.0.1:{api_port}/api/tools", api)
                    with ContextGraph(f"http://127.0.0.1:{api_port}") as graph:
                        report = graph.add_mcp_server("factory", sys.executable, [str(MCP_FIXTURE)],
                            catalog="ops", timeout=20)
                        initial_version = report.catalog_version
                        original_refund = graph.inspect_tool("ops.factory.check_refund")
                        assert original_refund.dependencies == []
                        dependency_registration = graph.register_tools("ops", [{
                            "name": "factory.check_refund", "prerequisites": ["factory.get_order"],
                        }])
                        with_dependency_version = dependency_registration["catalog_version"]
                        persisted_version, persisted_fingerprint = _read_catalog_record(ports["http"], token)
                        assert persisted_version == with_dependency_version
                        assert persisted_fingerprint == dependency_registration["fingerprint"]
                        dependency_refund = graph.inspect_tool("ops.factory.check_refund")
                        assert dependency_refund.description == original_refund.description
                        assert dependency_refund.input_schema == original_refund.input_schema
                        dependency_result = graph.retrieve("Determine refund eligibility for this order",
                            catalog="ops", max_tools=4)
                        assert any(dep.source_tool_id == "ops.factory.check_refund"
                            and dep.required_tool_id == "ops.factory.get_order"
                            for dep in dependency_result.dependencies)
                        known_dependency_result = graph.retrieve(
                            "Determine refund eligibility for this order", catalog="ops", max_tools=4,
                            known_tool_ids=["ops.factory.get_order"])
                        known_nodes = [node for node in known_dependency_result.metadata.graph_nodes
                            if node.get("id") == "ops.factory.get_order"]
                        assert len(known_nodes) == 1
                        assert known_nodes[0]["type"] == "KnownTool"
                        assert [tool.id for tool in known_dependency_result.tools] == ["ops.factory.check_refund"]
                        assert known_dependency_result.metadata.bounded["selected_tools"] == 1
                        assert known_dependency_result.complete is True
                        assert {("ops.factory.check_refund", "ops.factory.get_order")} <= {
                            (edge.source_tool_id, edge.required_tool_id)
                            for edge in known_dependency_result.dependencies}

                        # MCP refresh omits prerequisite metadata, so it must
                        # preserve the explicit dependency and catalog identity.
                        refreshed = graph.add_mcp_server("factory", sys.executable, [str(MCP_FIXTURE)],
                            catalog="ops", timeout=20)
                        assert refreshed.catalog_version == with_dependency_version
                        refreshed_refund = graph.inspect_tool("ops.factory.check_refund")
                        assert refreshed_refund.dependencies == ["ops.factory.get_order"]

                        # Explicit [] means clear; restoring the edge restores
                        # the exact previous full-catalog fingerprint.
                        cleared = graph.register_tools("ops", [{
                            "name": "factory.check_refund", "prerequisites": [],
                        }])
                        assert cleared["catalog_version"] != with_dependency_version
                        assert graph.inspect_tool("ops.factory.check_refund").dependencies == []
                        restored = graph.register_tools("ops", [{
                            "name": "factory.check_refund", "prerequisites": ["factory.get_order"],
                        }])
                        assert restored["catalog_version"] == with_dependency_version

                        # A partial schema refresh changes the full catalog
                        # fingerprint while retaining all omitted catalog tools.
                        schema_update = graph.register_tools("ops", [{
                            "name": "factory.get_order", "server": "factory",
                            "description": "Read an order using an identifier",
                            "input_schema": {"type": "object", "properties": {
                                "order_id": {"type": "integer"}}},
                        }])
                        assert schema_update["catalog_version"] != restored["catalog_version"]
                        all_ids = {tool.id for tool in graph.list_tools()}
                        assert "ops.factory.check_refund" in all_ids
                        assert "ops.factory.get_order" in all_ids
                        assert len([tool_id for tool_id in all_ids
                            if tool_id.startswith("ops.factory.")]) == 5

                        unrelated_catalog = graph.register_tools("unrelated", [{
                            "name": "audit.read_record", "server": "audit",
                            "description": "Read an unrelated audit record",
                            "input_schema": {"type": "object"},
                        }])
                        unrelated_before = _read_catalog_record(ports["http"], token, "unrelated")
                        assert unrelated_before == (unrelated_catalog["catalog_version"],
                            unrelated_catalog["fingerprint"])
                        ops_before_reassign = {tool.id: tool for tool in graph.list_tools()
                            if tool.catalog == "ops"}
                        version_before_reassign = schema_update["catalog_version"]
                        provider_rows_before = _hydra_query(ports["http"], token,
                            "MATCH (t:Tool {catalog: 'ops'})-[:PROVIDED_BY]->(s:MCPServer) "
                            "RETURN t.key AS tool,s.key AS server LIMIT 100")
                        reassigned = graph.register_tools("ops", [{
                            "name": "factory.check_refund", "server": "factory_v2",
                        }])
                        assert reassigned["catalog_version"] != version_before_reassign
                        check_refund_after = graph.inspect_tool("ops.factory.check_refund")
                        assert check_refund_after.server == "ops.factory_v2"
                        assert check_refund_after.dependencies == ["ops.factory.get_order"]
                        provider_edges = _hydra_query(ports["http"], token,
                            "MATCH (t:Tool {key: 'ops.factory.check_refund'})-[:PROVIDED_BY]->(s:MCPServer) "
                            "RETURN s.key AS server LIMIT 10")
                        assert provider_edges == [["ops.factory_v2"]]
                        provider_rows_after = _hydra_query(ports["http"], token,
                            "MATCH (t:Tool {catalog: 'ops'})-[:PROVIDED_BY]->(s:MCPServer) "
                            "RETURN t.key AS tool,s.key AS server LIMIT 100")
                        expected_provider_rows = sorted((tool, "ops.factory_v2" if tool == "ops.factory.check_refund" else server)
                            for tool, server in provider_rows_before)
                        assert sorted(map(tuple, provider_rows_after)) == expected_provider_rows
                        ops_after_reassign = {tool.id: tool for tool in graph.list_tools()
                            if tool.catalog == "ops"}
                        for tool_id, old_tool in ops_before_reassign.items():
                            if tool_id == "ops.factory.check_refund":
                                continue
                            new_tool = ops_after_reassign[tool_id]
                            assert new_tool.model_dump() == old_tool.model_dump()
                        assert _read_catalog_record(ports["http"], token, "unrelated") == unrelated_before
                        repeated_refresh = graph.register_tools("ops", [{
                            "name": "factory.check_refund", "server": "factory_v2",
                        }])
                        assert repeated_refresh["catalog_version"] == reassigned["catalog_version"]
                        provider_edges_after_refresh = _hydra_query(ports["http"], token,
                            "MATCH (t:Tool {key: 'ops.factory.check_refund'})-[:PROVIDED_BY]->(s:MCPServer) "
                            "RETURN s.key AS server LIMIT 10")
                        assert provider_edges_after_refresh == [["ops.factory_v2"]]

                        refund = graph.retrieve("Determine refund eligibility for this order",
                            catalog="ops", max_tools=4)
                        control = graph.retrieve("Find a compatible replacement part",
                            catalog="ops", max_tools=2)
                        evaluation = graph.evaluate("ops", [
                            {"task": "Determine refund eligibility for this order",
                             "required_tools": ["ops.factory.check_refund"]},
                            {"task": "Find a compatible replacement part",
                             "required_tools": ["ops.factory.list_replacement_parts"]},
                        ], max_tools=4)
                    refund_ids = {tool.id for tool in refund.tools}
                    control_ids = {tool.id for tool in control.tools}
                    assert report.discovered_tool_count == 5
                    assert initial_version != with_dependency_version
                    assert {"ops.factory.check_refund", "ops.factory.get_order"} <= refund_ids
                    assert any(dep.source_tool_id == "ops.factory.check_refund"
                        and dep.required_tool_id == "ops.factory.get_order" for dep in refund.dependencies)
                    assert refund.complete is True
                    assert control_ids == {"ops.factory.list_replacement_parts"}
                    assert refund.execution_performed is False
                    assert evaluation.graph_average_required_tool_recall == 1.0
                    assert "does not measure LLM accuracy" in evaluation.measurement_note
                    sdk_dependencies = {(dep.source_tool_id, dep.required_tool_id) for dep in refund.dependencies}

                    async def call_mcp_adapter():
                        params = StdioServerParameters(command=sys.executable,
                            args=["-m", "contextgraph_mcp.server"],
                            env={"CONTEXTGRAPH_API_URL": f"http://127.0.0.1:{api_port}"})
                        async with Client(params) as mcp_client:
                            routed = await mcp_client.call_tool("retrieve_context", {
                                "task": "Determine refund eligibility for this order", "catalog": "ops", "max_tools": 4})
                            inspected = await mcp_client.call_tool("inspect_tool", {"tool_id": "ops.factory.check_refund"})
                            return routed.structured_content, inspected.structured_content

                    mcp_result, inspected = asyncio.run(call_mcp_adapter())
                    mcp_ids = {tool["id"] for tool in mcp_result["selected_tools"]}
                    mcp_dependencies = {(edge["from"], edge["to"]) for edge in mcp_result["required_dependencies"]}
                    assert mcp_ids == refund_ids
                    assert mcp_dependencies == sdk_dependencies
                    assert mcp_result["execution_performed"] is False
                    assert inspected["prerequisites"] == ["ops.factory.get_order"]
            finally:
                if api is not None and api.poll() is None:
                    api.terminate()
                    api.wait(timeout=10)
                hydra.terminate()
                hydra.wait(timeout=10)

