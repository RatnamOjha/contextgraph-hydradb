from __future__ import annotations

import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .fixtures import SKILLS, TASKS, TRAINING_TASKS, make_catalog
from .hydra import HydraClient, HydraError, cypher_string
from .registry import inspect_tools, seed_catalog
from .router import classify, execute_fixture, flat_retrieve, route_graph, tool_json

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("contextgraph")
db = HydraClient()


class SeedRequest(BaseModel):
    catalog_size: Literal[20, 100, 500] = 20
    reset: bool = True


class RunRequest(BaseModel):
    task: str = Field(min_length=8, max_length=2000)
    method: Literal["full", "flat", "graph", "ablation"] = "graph"
    catalog_size: Literal[20, 100, 500] = 20


class EvalRequest(BaseModel):
    catalog_size: Literal[20, 100, 500] = 100
    iterations: int = Field(default=3, ge=1, le=6)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await db.open()
    yield
    await db.close()


app = FastAPI(title="ContextGraph", version="0.1.0", lifespan=lifespan)
WEB = Path(__file__).resolve().parents[1] / "web"


def as_http_error(exc: Exception) -> HTTPException:
    return HTTPException(503, detail=str(exc))


@app.get("/")
async def home():
    return FileResponse(WEB / "index.html")


@app.get("/api/health")
async def health():
    try:
        _, _, latency = await db.query(f"MATCH (n:Agent {{id: {db.id_for('Agent', 'support-agent')}}}) RETURN n.key AS key")
        return {"ok": True, "hydradb": "connected", "probe_ms": round(latency, 2)}
    except Exception as exc:
        raise HTTPException(status_code=503, detail={"ok": False, "hydradb": str(exc)}) from exc


@app.get("/api/ready")
async def ready():
    try:
        _, rows, _ = await db.query(f"MATCH (a:Agent {{id: {db.id_for('Agent', 'support-agent')}}}) RETURN a.key AS key")
        if not rows:
            raise HTTPException(status_code=503, detail="Demo catalog is still initializing")
        return {"ok": True, "catalog": "ready"}
    except HydraError as exc:
        raise as_http_error(exc) from exc


@app.post("/api/seed")
async def seed(request: SeedRequest):
    try:
        result = await seed_catalog(db, request.catalog_size, reset=request.reset)
        log.info("catalog_seeded %s", result)
        return {"status": "seeded", "catalog_size": request.catalog_size, **result}
    except Exception as exc:
        raise as_http_error(exc) from exc


@app.get("/api/tools")
async def tools():
    try:
        return {"tools": await inspect_tools(db)}
    except Exception as exc:
        raise as_http_error(exc) from exc


async def record_execution(scenario: str, task_text: str, method: str, success: bool, selected: list[str], resolved: list[dict], duration_ms: float) -> None:
    execution_id = int(time.time_ns() & ((1 << 62) - 1)) or 1
    task_id = int(time.time_ns() & ((1 << 62) - 1)) or 2
    type_id = db.id_for("TaskType", scenario)
    query = (
        f"CREATE (e:Execution {{id: {execution_id}, method: {cypher_string(method)}, success: {str(success).lower()}, duration_ms: {duration_ms:.3f}}})"
        f"-[:FOR_TASK]->(t:Task {{id: {task_id}, prompt: {cypher_string(task_text)}, intent: {cypher_string(scenario)}}})"
    )
    await db.query(query)
    await db.query(f"MATCH (task:Task {{id: {task_id}}}) CREATE (task)-[:CLASSIFIED_AS]->(type:TaskType {{id: {type_id}}})")
    if selected:
        rows = [{"src": execution_id, "dst": db.id_for("Tool", key)} for key in selected]
        await db.query("UNWIND $rows AS row CREATE (e {id: row.src})-[:SELECTED]->(t {id: row.dst})", {"rows": rows})
    if resolved:
        rows = [{"src": execution_id, "dst": db.id_for("Entity", f"{row['type']}:{row['id']}")} for row in resolved]
        await db.query("UNWIND $rows AS row CREATE (e {id: row.src})-[:RESOLVED]->(n {id: row.dst})", {"rows": rows})


@app.post("/api/run")
async def run_task(request: RunRequest):
    started = time.perf_counter()
    scenario = classify(request.task)
    fixture = TASKS[scenario]
    catalog = make_catalog(request.catalog_size)
    retrieval_ms = 0.0
    resolved: list[dict] = []
    paths: list[dict[str, str]] = []
    chosen_skill = None
    if request.method == "full":
        selected = catalog
        retrieval_ms = 0.0
    elif request.method == "flat":
        t0 = time.perf_counter()
        selected = flat_retrieve(request.task, catalog)
        retrieval_ms = (time.perf_counter() - t0) * 1000
    elif request.method == "ablation":
        # Same registered HydraDB catalog and tool metadata, but no relationship
        # traversal: scan tools, then apply the flat lexical ranker.
        try:
            t0 = time.perf_counter()
            graph_catalog = await inspect_tools(db)
            db_ms = (time.perf_counter() - t0) * 1000
            by_key = {tool.key: tool for tool in catalog}
            selected = flat_retrieve(request.task, [by_key[row["key"]] for row in graph_catalog if row["key"] in by_key])
            retrieval_ms = db_ms
        except Exception as exc:
            raise as_http_error(exc) from exc
    else:
        try:
            route = await route_graph(db, scenario)
            chosen_skill = route["skill"]
            selected = [next((t for t in catalog if t.key == row["key"]), None) for row in route["tools"]]
            selected = [t for t in selected if t is not None]
            resolved = route["resolved_entities"]
            paths = route["edges"]
            retrieval_ms = route["retrieval_ms"]
        except Exception as exc:
            raise as_http_error(exc) from exc

    selected_keys = {tool.key for tool in selected}
    result = execute_fixture(scenario, request.method, selected_keys, resolved if request.method == "graph" else [])
    result["trace"].insert(0, {"step": "task-analysis", "message": f"Classified as {scenario}: {fixture['title']}"})
    context_json = json.dumps([tool_json(tool) for tool in selected], separators=(",", ":"))
    context_tokens = max(1, len(context_json) // 4)
    end_to_end_ms = (time.perf_counter() - started) * 1000
    try:
        await record_execution(scenario, request.task, request.method, result["success"], [row["tool"] for row in result["tool_outputs"]], result["reused_entities"], end_to_end_ms)
    except Exception as exc:
        log.exception("execution_trace_write_failed")
        result["trace"].append({"step": "trace-warning", "message": f"Could not persist execution trace: {exc}"})
    end_to_end_ms = (time.perf_counter() - started) * 1000
    reused = {"customer.resolve", "orders.list"} if resolved else set()
    primary = set(fixture["required"]) - reused
    precision = len(primary & selected_keys) / max(1, len(selected_keys))
    recall = len(primary & selected_keys) / max(1, len(primary))
    return {
        "scenario": scenario,
        "task": request.task,
        "method": request.method,
        "skill": chosen_skill,
        "selected_tools": [tool_json(tool) for tool in selected],
        "paths": paths,
        "resolved_entities": resolved if request.method == "graph" else [],
        "metrics": {
            "task_success": result["success"],
            "tool_precision": round(precision, 3),
            "tool_recall": round(recall, 3),
            "context_tokens_est": context_tokens,
            "retrieval_ms": round(retrieval_ms, 3),
            "end_to_end_ms": round(end_to_end_ms, 3),
            "unnecessary_tools": max(0, len(selected_keys - primary)),
            "tool_calls": result["tool_calls"],
            "dependency_success": not result["missing_tools"] and not result["dependency_failures"],
        },
        "trace": result["trace"],
        "tool_outputs": result["tool_outputs"],
        "answer": result["answer"],
    }


def cypher_weight(value: float) -> str:
    return f"{value:.4f}"


async def adjust_skill_edge(scenario: str, skill: str, value: float) -> None:
    task_id = db.id_for("TaskType", scenario)
    skill_id = db.id_for("Skill", skill)
    await db.query(
        f"MATCH (t:TaskType {{id: {task_id}}})-[r:REQUIRES]->(s:Skill {{id: {skill_id}}}) SET r.weight = {cypher_weight(value)}"
    )


@app.post("/api/evaluate")
async def evaluate(request: EvalRequest):
    try:
        catalog = make_catalog(request.catalog_size)
        required = {key: value["skill"] for key, value in TASKS.items()}
        history: list[dict] = []
        async def score() -> dict:
            correct = total = 0
            successful = 0
            recall_total = 0.0
            token_total = 0
            for scenario, expected_skill in required.items():
                route = await route_graph(db, scenario)
                correct += route["skill"] == expected_skill
                total += 1
                route_keys = {tool["key"] for tool in route["tools"]}
                result = execute_fixture(scenario, "graph", route_keys, route["resolved_entities"])
                successful += result["success"]
                needed = set(TASKS[scenario]["required"])
                if route["resolved_entities"]:
                    needed -= {"customer.resolve", "orders.list"}
                recall_total += len(needed & route_keys) / max(1, len(needed))
                token_total += max(1, sum(len(json.dumps(t, separators=(",", ":"))) for t in route["tools"]) // 4)
            return {"accuracy": correct / total, "task_success_rate": successful / total, "tool_recall": recall_total / total, "context_tokens_est": token_total / total}

        # Reinitialize only the candidate-ranking edges so this benchmark is
        # repeatable. REQUIRES/USES/DEPENDS_ON catalog edges stay untouched.
        for scenario in TRAINING_TASKS:
            task_id = db.id_for("TaskType", scenario)
            for skill in SKILLS:
                await adjust_skill_edge(scenario, skill, 1.0)
        initial = await score()
        frozen_baseline = {
            "accuracy": initial["accuracy"],
            "task_success_rate": initial["task_success_rate"],
            "tool_recall": initial["tool_recall"],
            "context_tokens_est": sum(len(json.dumps(tool_json(t), separators=(",", ":"))) for t in catalog) // 4,
        }
        history.append({"iteration": 0, **initial, "updates": 0})
        for iteration in range(1, request.iterations + 1):
            updates = 0
            training_successes = 0
            # Disjoint training prompts, entity IDs, and execution IDs; only the
            # shared TaskType -> Skill relationship receives feedback.
            for scenario, training in TRAINING_TASKS.items():
                expected_skill = training["skill"]
                route = await route_graph(db, scenario)
                available_tools = {tool["key"] for tool in route["tools"]}
                training_success = set(training["required_tools"]).issubset(available_tools)
                training_successes += training_success
                current = 1.0
                _, rows, _ = await db.query(
                    f"MATCH (t:TaskType {{id: {db.id_for('TaskType', scenario)}}})-[r:REQUIRES]->(s:Skill) RETURN s.key AS skill, r.weight AS weight"
                )
                for skill, weight in rows:
                    if skill == expected_skill:
                        current = float(weight)
                # Successful path reinforcement, or a retrieval-miss correction
                # when a required training tool is absent from the subgraph.
                reward = 0.4 if training_success else 0.6
                await adjust_skill_edge(scenario, expected_skill, current + reward)
                updates += 1
            result = await score()
            history.append({"iteration": iteration, **result, "training_success_rate": training_successes / len(TRAINING_TASKS), "updates": updates})
        return {"history": history, "frozen_baseline": frozen_baseline, "training_fixtures": [row["prompt"] for row in TRAINING_TASKS.values()], "held_out": [row["task"] for row in TASKS.values()], "feedback": "successful route reinforcement and training-only retrieval-miss correction"}
    except Exception as exc:
        raise as_http_error(exc) from exc


@app.get("/api/graph")
async def graph(scenario: str = "refund"):
    try:
        route = await route_graph(db, scenario if scenario in TASKS else "refund")
        nodes: dict[str, dict] = {}
        tool_keys = {tool["key"] for tool in route["tools"]}
        path_tool_keys = {edge[key] for edge in route["edges"] if edge["type"] in {"USES", "DEPENDS_ON"} for key in ("from", "to")}
        skill_keys = {edge["to"] for edge in route["edges"] if edge["type"] == "REQUIRES"}
        mcp_keys = {tool.get("mcp") for tool in route["tools"] if tool.get("mcp")}
        for edge in route["edges"]:
            for key in (edge["from"], edge["to"]):
                if key in nodes:
                    continue
                kind = "TaskType" if key == scenario else "Skill" if key in skill_keys else "Tool" if key in (tool_keys | path_tool_keys) else "MCPServer" if key in mcp_keys else "Entity"
                nodes[key] = {"id": key, "label": key, "type": kind}
        for entity in route["resolved_entities"]:
            key = f"{entity['type']}:{entity['id']}"
            nodes[key] = {"id": key, "label": key, "type": "Entity"}
        return {"scenario": scenario, "skill": route["skill"], "nodes": list(nodes.values()), "edges": route["edges"], "resolved_entities": route["resolved_entities"]}
    except Exception as exc:
        raise as_http_error(exc) from exc
