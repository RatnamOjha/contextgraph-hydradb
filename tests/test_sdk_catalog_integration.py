"""End-to-end registration/retrieval against a disposable local HydraDB graph."""
import json
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
                        graph.register_tools("ops", [{
                            "name": "factory.check_refund", "server": "factory",
                            "description": "Check refund eligibility for an order",
                            "input_schema": {"type": "object"},
                            "prerequisites": ["factory.get_order"],
                        }])
                        refund = graph.retrieve("Determine refund eligibility for this order",
                            catalog="ops", max_tools=4)
                        control = graph.retrieve("Find a compatible replacement part",
                            catalog="ops", max_tools=2)
                    refund_ids = {tool.id for tool in refund.tools}
                    control_ids = {tool.id for tool in control.tools}
                    assert report.discovered_tool_count == 5
                    assert {"ops.factory.check_refund", "ops.factory.get_order"} <= refund_ids
                    assert any(dep.source_tool_id == "ops.factory.check_refund"
                        and dep.required_tool_id == "ops.factory.get_order" for dep in refund.dependencies)
                    assert refund.complete is True
                    assert control_ids == {"ops.factory.list_replacement_parts"}
                    assert refund.execution_performed is False
            finally:
                if api is not None and api.poll() is None:
                    api.terminate()
                    api.wait(timeout=10)
                hydra.terminate()
                hydra.wait(timeout=10)

