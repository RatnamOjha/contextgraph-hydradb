#!/usr/bin/env python3
"""Run a reversible graph-learning experiment in a fresh, temporary HydraDB store."""
from __future__ import annotations

import argparse
import hashlib
import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
HYDRA_SOURCE = ROOT / "hydradb-upstream"
NODE = HYDRA_SOURCE / "target/debug/graph-node"
sys.path.insert(0, str(ROOT))

from backend.dataset_workflow import execute_deterministic
from backend.fixtures import DATA_TOOLS
from backend.hydra import HydraClient, HydraError
from backend.objectives import evaluate_answer
from backend.olist_store import list_orders
from backend.router import candidate_skills, flat_candidates, tool_json


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def isolated_env(store: Path, token_file: Path) -> tuple[dict[str, str], dict[str, int]]:
    http, bolt, admin = free_port(), free_port(), free_port()
    env = os.environ.copy()
    env.update({
        "CLOUD_PROVIDER": "local", "LOCAL_PATH": str(store / "store"),
        "GRAPH_NAMESPACE": "contextgraph", "GRAPH_ID": "default", "GRAPH_CELL_ID": "cell-0",
        "GRAPH_CELLS": "cell-0", "GRAPH_DATA_PATH": "data", "GRAPH_NODE_ID": "node-0",
        "GRAPH_BOLT_NODE_ADDRESSES": f"node-0=127.0.0.1:{bolt}",
        "GRAPH_BOLT_ADDR": f"127.0.0.1:{bolt}", "GRAPH_ADVERTISED_BOLT_ADDR": f"127.0.0.1:{bolt}",
        "GRAPH_HTTP_ADDR": f"127.0.0.1:{http}", "GRAPH_ADMIN_ADDR": f"127.0.0.1:{admin}",
        "GRAPH_AUTH_TOKEN_FILE": str(token_file), "GRAPH_ALLOW_PLAINTEXT": "true",
        "GRAPH_DATA_CACHE_DIR": str(store / "cache"), "GRAPH_DATA_CACHE_BYTES": "33554432",
        "GRAPH_USAGE_REPORT_ENABLED": "false", "RUST_MIN_STACK": "33554432", "RUST_LOG": "warn",
    })
    return env, {"http": http, "bolt": bolt, "admin": admin}


def wait_ready(process: subprocess.Popen, admin_port: int, log_path: Path, timeout: float = 90) -> None:
    import urllib.request
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{admin_port}/readyz"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Isolated HydraDB exited; see {log_path}")
        try:
            with urllib.request.urlopen(url, timeout=1):
                return
        except Exception:
            time.sleep(0.25)
    raise TimeoutError(f"Isolated HydraDB did not become ready; see {log_path}")


async def query_state(db: HydraClient, src: int, dst: int) -> list[list[Any]]:
    _, rows, _ = await db.query(
        "MATCH (a:Tool {id: $src})-[r:DEPENDS_ON]->(b:Tool {id: $dst}) "
        "RETURN a.key AS tool,b.key AS dependency,r.weight AS weight",
        {"src": src, "dst": dst})
    return rows


async def update_learning_record(db: HydraClient, ident: int, state: str, evidence: dict[str, Any]) -> None:
    serialized = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    await db.query("MATCH (u:LearningUpdate {id: $id}) SET u.state=$state,u.evidence=$evidence",
        {"id": ident, "state": state, "evidence": serialized})
    _, rows, _ = await db.query("MATCH (u:LearningUpdate {id: $id}) RETURN u.state AS state,u.evidence AS evidence",
        {"id": ident})
    if not rows or rows[0] != [state, serialized]:
        raise RuntimeError("Learning evidence update failed exact HydraDB readback")


async def experiment(db: HydraClient, heldout_count: int) -> dict[str, Any]:
    runtime_probes: dict[str, Any] = {}
    try:
        await db.query("UNWIND $rows AS row MERGE (n {id: row.id}) SET n:Probe,n.state='literal'",
            {"rows": [{"id": db.id_for("Probe", "unsupported-literal-set")}]})
        raise RuntimeError("HydraDB unexpectedly accepted a literal UNWIND SET value")
    except HydraError as exc:
        error_text = str(exc)
        if "HTTP 400" not in error_text or "row map" not in error_text:
            raise
        runtime_probes["literal_unwind_set"] = {"http_status": 400,
            "error": error_text.split(": ", 1)[1]}
    orders = list_orders(100)
    if len(orders) < heldout_count + 1:
        raise RuntimeError("Olist subset does not have enough distinct orders")
    training = orders[0]
    heldout = orders[1:1 + heldout_count]
    training_id = training["order_id"]
    item_tool = next(tool for tool in DATA_TOOLS if tool.key == "order.items")
    details_tool = next(tool for tool in DATA_TOOLS if tool.key == "order.details")
    dependency = "order.details"
    if dependency not in item_tool.dependencies:
        raise RuntimeError("Proposed relationship is not declared in Tool.dependencies")

    src, dst = db.id_for("Tool", item_tool.key), db.id_for("Tool", details_tool.key)
    rows = [
        {"id": src, "key": item_tool.key, "dependencies_json": json.dumps(list(item_tool.dependencies))},
        {"id": dst, "key": details_tool.key, "dependencies_json": json.dumps(list(details_tool.dependencies))},
    ]
    await db.query("UNWIND $rows AS row MERGE (n {id: row.id}) "
                   "SET n:Tool,n.key=row.key,n.dependencies_json=row.dependencies_json", {"rows": rows})
    try:
        await db.query("MATCH (t:Tool {id: $id}) SET t.null_probe=$value", {"id": src, "value": None})
        raise RuntimeError("HydraDB unexpectedly accepted a null property parameter")
    except HydraError as exc:
        error_text = str(exc)
        if "HTTP 400" not in error_text or "invalid_parameter" not in error_text:
            raise
        runtime_probes["null_property_parameter"] = {"http_status": 400,
            "error": error_text.split(": ", 1)[1]}
    initial_edges = await query_state(db, src, dst)
    if initial_edges:
        raise RuntimeError("Isolated training graph unexpectedly already contains the prerequisite edge")
    _, training_tool_rows, _ = await db.query("MATCH (t:Tool {id: $id}) RETURN t.key AS key", {"id": src})
    if not training_tool_rows or training_tool_rows[0][0] != item_tool.key:
        raise RuntimeError("Training tool was not retrieved from the isolated HydraDB graph")

    training_task = f"List the item categories for reference {training_id}."
    training_run = execute_deterministic(training_task, training_id, [item_tool])
    if not any(f.get("tool") == item_tool.key and f.get("missing_dependency") == dependency
               for f in training_run["dependency_failures"]):
        raise RuntimeError("Training execution did not observe the declared dependency failure")

    baseline = {"tools": sorted([item_tool.key, details_tool.key]), "edges": initial_edges}
    version = hashlib.sha256(json.dumps(baseline, sort_keys=True).encode()).hexdigest()
    run_key = f"{version}|{training_id}|{heldout[0]['order_id']}"
    update_id = db.id_for("LearningUpdate", run_key)
    evidence: dict[str, Any] = {
        "original_task": training_task,
        "training_order_id": training_id,
        "retrieval_snapshot": {"selected_tools": [item_tool.key], "edges": initial_edges},
        "actual_execution_failure": {"dependency_failures": training_run["dependency_failures"],
            "tool_outputs": training_run["tool_outputs"]},
        "proposed_graph_correction": {"tool": item_tool.key, "relationship_type": "DEPENDS_ON",
            "dependency": dependency, "autonomous_discovery": False},
        "validation_evidence": {"declared_dependencies": list(item_tool.dependencies),
            "relationship_validated": dependency in item_tool.dependencies},
        "graph_version": version,
        "heldout_retest": [],
        "rollback_information": {"initial_edge_count": 0},
    }
    serialized = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    await db.query("UNWIND $rows AS row MERGE (u {id: row.id}) SET u:LearningUpdate,u.tool=row.tool,"
                   "u.dependency=row.dependency,u.relationship_type=row.relationship_type,u.graph_version=row.graph_version,"
                   "u.state=row.state,u.evidence=row.evidence",
        {"rows": [{"id": update_id, "tool": item_tool.key, "dependency": dependency,
            "relationship_type": "DEPENDS_ON", "graph_version": version,
            "state": "observed_dependency_failure", "evidence": serialized}]})
    _, record_rows, _ = await db.query("MATCH (u:LearningUpdate {id: $id}) RETURN u.state AS state,u.evidence AS evidence",
        {"id": update_id})
    if not record_rows or record_rows[0] != ["observed_dependency_failure", serialized]:
        raise RuntimeError("Initial learning evidence failed exact HydraDB readback")

    await db.query("UNWIND $rows AS row CREATE (a {id: row.src})-[:DEPENDS_ON]->(b {id: row.dst})",
                   {"rows": [{"src": src, "dst": dst}]})
    await db.query("MATCH (a:Tool {id: $src})-[r:DEPENDS_ON]->(b:Tool {id: $dst}) SET r.weight=$weight",
                   {"src": src, "dst": dst, "weight": 1.0})
    corrected_edges = await query_state(db, src, dst)
    if corrected_edges != [[item_tool.key, details_tool.key, 1.0]]:
        raise RuntimeError(f"Corrected graph failed relationship readback: {corrected_edges!r}")

    corrected_keys = {item_tool.key, details_tool.key}
    corrected_tools = [tool for tool in DATA_TOOLS if tool.key in corrected_keys]
    before_rows, after_rows, flat_rows, expanded_rows = [], [], [], []
    for index, order in enumerate(heldout):
        # Run the held-out baseline while the edge is absent, then retrieve the
        # prerequisite from the corrected graph without a request filter.
        task = f"Which goods categories appear in historical order {order['order_id']}?"
        _, initial_tools, _ = await db.query("MATCH (t:Tool {id: $id}) RETURN t.key AS key", {"id": src})
        initial_keys = {row[0] for row in initial_tools}
        initial_selected = [tool for tool in DATA_TOOLS if tool.key in initial_keys]
        initial = execute_deterministic(task, order["order_id"], initial_selected)
        initial_outcome = evaluate_answer(task, initial["answer"], initial["tool_outputs"], entity_resolved=True)
        _, graph_tools, _ = await db.query("MATCH (t:Tool) RETURN t.key AS key")
        graph_edges = await query_state(db, src, dst)
        keys = {row[0] for row in graph_tools}
        retrieved_keys = {item_tool.key}
        if graph_edges and item_tool.key in keys and details_tool.key in keys:
            retrieved_keys.add(details_tool.key)
        selected = [tool for tool in DATA_TOOLS if tool.key in retrieved_keys]
        corrected = execute_deterministic(task, order["order_id"], selected)
        corrected_outcome = evaluate_answer(task, corrected["answer"], corrected["tool_outputs"], entity_resolved=True)
        candidates = candidate_skills(task)
        lexical_tools = flat_candidates(task, candidates, expand=False)
        expanded_tools = flat_candidates(task, candidates, expand=True)
        lexical = execute_deterministic(task, order["order_id"], lexical_tools)
        expanded = execute_deterministic(task, order["order_id"], expanded_tools)
        lexical_outcome = evaluate_answer(task, lexical["answer"], lexical["tool_outputs"], entity_resolved=True)
        expanded_outcome = evaluate_answer(task, expanded["answer"], expanded["tool_outputs"], entity_resolved=True)
        before_rows.append({"order_id": order["order_id"], "task": task,
            "success": initial_outcome["code"] == "SUCCESS", "outcome": initial_outcome,
            "dependency_failures": initial["dependency_failures"], "tool_calls": initial["tool_calls"],
            "context_tokens_est": sum(len(json.dumps(tool_json(tool), separators=(",", ":"))) for tool in initial_selected) // 4})
        after_rows.append({"order_id": order["order_id"], "task": task,
            "success": corrected_outcome["code"] == "SUCCESS", "outcome": corrected_outcome,
            "dependency_failures": corrected["dependency_failures"], "tool_calls": corrected["tool_calls"],
            "retrieved_tools": sorted(retrieved_keys),
            "context_tokens_est": sum(len(json.dumps(tool_json(tool), separators=(",", ":"))) for tool in selected) // 4})
        flat_rows.append({"order_id": order["order_id"], "success": lexical_outcome["code"] == "SUCCESS",
            "dependency_failures": lexical["dependency_failures"], "tool_calls": lexical["tool_calls"],
            "context_tokens_est": sum(len(json.dumps(tool_json(tool), separators=(",", ":"))) for tool in lexical_tools) // 4})
        expanded_rows.append({"order_id": order["order_id"], "success": expanded_outcome["code"] == "SUCCESS",
            "dependency_failures": expanded["dependency_failures"], "tool_calls": expanded["tool_calls"],
            "context_tokens_est": sum(len(json.dumps(tool_json(tool), separators=(",", ":"))) for tool in expanded_tools) // 4})
    evidence["heldout_retest"] = after_rows
    evidence["correction_status"] = "applied_and_validated"
    await update_learning_record(db, update_id, "validated", evidence)

    await db.query("MATCH (a:Tool {id: $src})-[r:DEPENDS_ON]->(b:Tool {id: $dst}) DELETE r",
                   {"src": src, "dst": dst})
    restored_edges = await query_state(db, src, dst)
    if restored_edges != initial_edges:
        raise RuntimeError(f"Graph rollback did not restore baseline: {restored_edges!r}")
    evidence["rollback_information"] = {"initial_edges": initial_edges, "restored_edges": restored_edges,
        "restored": restored_edges == initial_edges}
    evidence["correction_status"] = "rolled_back_after_validation"
    await update_learning_record(db, update_id, "rolled_back", evidence)

    def rate(rows: list[dict[str, Any]]) -> float:
        return round(sum(row["success"] for row in rows) / len(rows), 3) if rows else 0.0
    return {
        "experiment": "isolated_hydradb_dependency_graph_learning_v1",
        "runtime_probes": runtime_probes,
        "training_order_id": training_id,
        "heldout_order_ids": [row["order_id"] for row in heldout],
        "heldout_disjoint": all(row["order_id"] != training_id for row in heldout),
        "initial_graph": {"edges": initial_edges, "heldout_completion": rate(before_rows),
            "dependency_failures": sum(len(row["dependency_failures"]) for row in before_rows),
            "tool_calls": sum(row["tool_calls"] for row in before_rows),
            "mean_context_tokens_est": round(sum(row["context_tokens_est"] for row in before_rows) / len(before_rows), 1)},
        "corrected_graph": {"edges": corrected_edges, "heldout_completion": rate(after_rows),
            "dependency_failures": sum(len(row["dependency_failures"]) for row in after_rows),
            "tool_calls": sum(row["tool_calls"] for row in after_rows),
            "mean_context_tokens_est": round(sum(row["context_tokens_est"] for row in after_rows) / len(after_rows), 1)},
        "flat_lexical": {"heldout_completion": rate(flat_rows),
            "dependency_failures": sum(len(row["dependency_failures"]) for row in flat_rows),
            "tool_calls": sum(row["tool_calls"] for row in flat_rows),
            "mean_context_tokens_est": round(sum(row["context_tokens_est"] for row in flat_rows) / len(flat_rows), 1)},
        "flat_dependency_expanded": {"heldout_completion": rate(expanded_rows),
            "dependency_failures": sum(len(row["dependency_failures"]) for row in expanded_rows),
            "tool_calls": sum(row["tool_calls"] for row in expanded_rows),
            "mean_context_tokens_est": round(sum(row["context_tokens_est"] for row in expanded_rows) / len(expanded_rows), 1)},
        "validated_corrections": 1,
        "regressions": sum(before["success"] and not after["success"] for before, after in zip(before_rows, after_rows)),
        "training_failure": training_run["dependency_failures"],
        "heldout_initial": before_rows,
        "heldout_corrected": after_rows,
        "rollback": {"restored_edges": restored_edges, "graph_restored": restored_edges == initial_edges},
        "learning_record": {"id": update_id, "state": "rolled_back", "readback_confirmed": True},
        "real_model_calls": 0,
        "limitation": "The corrected dependency was validated against declared Tool.dependencies metadata; it was not autonomously discovered. Execution used the deterministic Olist tool runner.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--heldout", type=int, choices=range(1, 10), default=3,
        help="number of distinct held-out Olist orders (default: 3)")
    args = parser.parse_args()
    if not NODE.is_file():
        raise SystemExit("HydraDB graph-node binary is missing. Build hydradb-upstream with --features server-runtime first.")
    with tempfile.TemporaryDirectory(prefix="contextgraph-learning-") as tmp:
        store = Path(tmp)
        (store / "store").mkdir()
        (store / "cache").mkdir()
        token_file = store / "auth-token"
        token = secrets.token_urlsafe(40)
        token_file.write_text(token)
        token_file.chmod(0o600)
        env, ports = isolated_env(store, token_file)
        log_path = store / "hydradb.log"
        with log_path.open("wb") as log_file:
            process = subprocess.Popen([str(NODE)], cwd=HYDRA_SOURCE, env=env,
                                       stdout=log_file, stderr=subprocess.STDOUT)
            try:
                wait_ready(process, ports["admin"], log_path)
                os.environ["HYDRA_URL"] = f"http://127.0.0.1:{ports['http']}"
                os.environ["HYDRA_GRAPH"] = "default"
                os.environ["HYDRA_NAMESPACE"] = "contextgraph"
                os.environ["HYDRA_CELL"] = "cell-0"
                os.environ["HYDRA_TOKEN"] = token
                db = HydraClient()
                report = asyncio.run(experiment(db, args.heldout))
                print(json.dumps(report, ensure_ascii=False, indent=2))
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    main()
