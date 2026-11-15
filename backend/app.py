from __future__ import annotations

import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .fixtures import EVALUATION_CASES, SKILLS, TASKS, TRAINING_TASKS, make_catalog
from .hydra import HydraClient, HydraError
from .registry import inspect_tools, seed_catalog
from .router import classify, execute_fixture, expanded_flat_retrieve, flat_retrieve, route_graph, tool_json

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("contextgraph")
db = HydraClient()


class SeedRequest(BaseModel):
    catalog_size: Literal[20, 100, 500] = 20
    reset: bool = True


class RunRequest(BaseModel):
    task: str = Field(min_length=8, max_length=2000)
    method: Literal["full", "flat", "graph", "ablation", "expanded_flat"] = "graph"
    catalog_size: Literal[20, 100, 500] = 20
    execution_mode: Literal["fixture", "agent"] = "fixture"


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


@app.get("/api/agent/status")
async def agent_status():
    return {"available": bool(os.getenv("ANTHROPIC_API_KEY")), "provider": "Anthropic", "model": os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")}


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
    await db.query(
        "UNWIND $rows AS row MERGE (e {id: row.id}) "
        "SET e:Execution, e.method = row.method, e.success = row.success, e.duration_ms = row.duration_ms",
        {"rows": [{"id": execution_id, "method": method, "success": success, "duration_ms": duration_ms}]},
    )
    await db.query(
        "UNWIND $rows AS row MERGE (t {id: row.id}) SET t:Task, t.prompt = row.prompt, t.intent = row.intent",
        {"rows": [{"id": task_id, "prompt": task_text, "intent": scenario}]},
    )
    await db.query(
        "UNWIND $rows AS row CREATE (task {id: row.src})-[:CLASSIFIED_AS]->(type {id: row.dst})",
        {"rows": [{"src": task_id, "dst": type_id}]},
    )
    await db.query(
        "UNWIND $rows AS row CREATE (e {id: row.src})-[:FOR_TASK]->(t {id: row.dst})",
        {"rows": [{"src": execution_id, "dst": task_id}]},
    )
    if selected:
        rows = [{"src": execution_id, "dst": db.id_for("Tool", key)} for key in selected]
        await db.query("UNWIND $rows AS row CREATE (e {id: row.src})-[:SELECTED]->(t {id: row.dst})", {"rows": rows})
    if resolved:
        rows = [{"src": execution_id, "dst": db.id_for("Entity", f"{row['type']}:{row['id']}")} for row in resolved]
        await db.query("UNWIND $rows AS row CREATE (e {id: row.src})-[:RESOLVED]->(n {id: row.dst})", {"rows": rows})


READ_ONLY_AGENT_TOOLS = {
    "ticket.lookup", "customer.resolve", "customer.profile", "orders.list", "order.get",
    "refund.eligibility", "invoice.lookup", "payment.history", "subscription.status",
    "shipment.track", "return.status", "policy.search", "shipment.address",
}


async def run_anthropic_agent(task: str, scenario: str, selected: list, resolved: list[dict]) -> dict:
    """Run a bounded Anthropic tool-use turn over graph-retrieved read tools."""
    import httpx

    model = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
    fixture = TASKS[scenario]
    resolved_map = {row["type"]: row["id"] for row in resolved}
    by_key = {tool.key: tool for tool in selected if tool.key in READ_ONLY_AGENT_TOOLS}
    tools = []
    for tool in by_key.values():
        tools.append({"name": tool.key.replace(".", "__"), "description": tool.description, "input_schema": json.loads(tool.input_schema)})
    trace = [{"step": "agent", "message": f"Anthropic {model} received {len(tools)} graph-retrieved read-only tool definitions"}]
    outputs: list[dict] = []
    called: set[str] = set()

    def execute(name: str, args: dict) -> dict:
        key = name.replace("__", ".")
        if key not in by_key:
            return {"error": "Tool is not available in the retrieved graph context."}
        tool = by_key[key]
        schema = json.loads(tool.input_schema)
        if not isinstance(args, dict) or set(args) - set(schema.get("properties", {})) or any(k not in args for k in schema.get("required", [])):
            return {"error": "Arguments do not match the tool input schema."}
        for arg, value in args.items():
            spec = schema["properties"][arg]
            if spec["type"] == "string" and not isinstance(value, str): return {"error": f"{arg} must be a string."}
            if spec["type"] == "integer" and (not isinstance(value, int) or isinstance(value, bool)): return {"error": f"{arg} must be an integer."}
            if spec["type"] == "number" and (not isinstance(value, (int, float)) or isinstance(value, bool)): return {"error": f"{arg} must be a number."}
            if spec["type"] == "boolean" and not isinstance(value, bool): return {"error": f"{arg} must be a boolean."}
        satisfied_by_entities = {"customer.resolve", "orders.list"} if resolved else set()
        blocked = [dep for dep in tool.dependencies if dep not in called and dep not in satisfied_by_entities]
        if blocked:
            return {"error": f"Unmet tool dependencies: {', '.join(blocked)}"}
        id_fields = {"ticket_id": ("ticket", fixture["ticket"]), "customer_id": ("customer", fixture["customer"]), "order_id": ("order", fixture["order"])}
        for field, (kind, known_fixture_id) in id_fields.items():
            if field in args and args[field] != known_fixture_id:
                return {"error": f"No fixture record exists for {field} {args[field]}"}
            if field in args and kind in resolved_map and args[field] != resolved_map[kind]:
                return {"error": f"{field} does not match the resolved HydraDB entity."}
        if key == "customer.resolve" and "reference" in args:
            ref = args["reference"]
            if re.fullmatch(r"[TCO]-[A-Za-z0-9]+", ref, re.I) and ref not in {fixture["ticket"], fixture["customer"], fixture["order"]}:
                return {"error": f"No fixture record exists for reference {ref}"}
        values = {
            "ticket.lookup": {"ticket_id": fixture["ticket"], "customer_id": fixture["customer"], "issue": "refund request / order support"},
            "customer.resolve": {"customer_id": fixture["customer"], "canonical": True},
            "customer.profile": {"customer_id": fixture["customer"], "tier": "standard", "status": "active"},
            "orders.list": {"customer_id": fixture["customer"], "orders": [fixture["order"]]},
            "order.get": {"order_id": fixture["order"], "status": "delivered", "total": 84.50},
            "refund.eligibility": {"order_id": fixture["order"], "eligible": fixture["eligible"], "policy_reason": "within policy window" if fixture["eligible"] else "outside policy window"},
            "invoice.lookup": {"customer_id": fixture["customer"], "invoice_id": "I-3401", "balance": 0},
            "payment.history": {"customer_id": fixture["customer"], "events": ["authorized", "captured"]},
            "subscription.status": {"customer_id": fixture["customer"], "status": "active", "plan": "standard"},
            "shipment.track": {"order_id": fixture["order"], "last_scan": "regional hub", "eta": "2026-11-16"},
            "return.status": {"order_id": fixture["order"], "return_authorized": False},
            "policy.search": {"category": args.get("category"), "excerpt": "Refund review requires checking delivery date and prior refund state."},
            "shipment.address": {"order_id": fixture["order"], "address_check": "available in fixture"},
        }
        called.add(key)
        return values[key]

    headers = {"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01", "content-type": "application/json"}
    body: dict = {"model": model, "max_tokens": 700, "system": "You are a support operations agent. Use only the supplied tools and their results. Never make changes, issue refunds, or invent unavailable records. State uncertainty and cite IDs from tool results.", "messages": [{"role": "user", "content": task}], "tools": tools}
    async with httpx.AsyncClient(timeout=45.0) as client:
        for turn in range(4):
            response = await client.post("https://api.anthropic.com/v1/messages", headers=headers, json=body)
            if response.status_code >= 400:
                raise HTTPException(502, detail="Anthropic agent request failed; check server configuration and provider status.")
            message = response.json()
            content = message.get("content", [])
            uses = [block for block in content if block.get("type") == "tool_use"]
            if not uses:
                answer = "\n".join(block.get("text", "") for block in content if block.get("type") == "text").strip()
                if not answer:
                    raise HTTPException(502, detail="Anthropic returned no final answer.")
                trace.append({"step": "agent-answer", "message": f"Model completed after {turn + 1} turn(s)"})
                needed = set(fixture["required"])
                if resolved:
                    needed -= {"customer.resolve", "orders.list"}
                success = bool(answer) and needed.issubset(called)
                if not success:
                    trace.append({"step": "task-check", "message": f"Required read-only calls missing: {', '.join(sorted(needed - called))}"})
                return {"success": success, "missing_tools": sorted(needed - called), "dependency_failures": [], "trace": trace, "answer": answer, "tool_calls": len(outputs), "tool_outputs": outputs, "reused_entities": resolved, "agent": {"provider": "Anthropic", "model": model, "turns": turn + 1, "usage": message.get("usage", {})}}
            body["messages"].append({"role": "assistant", "content": content})
            results = []
            for call in uses:
                result = execute(call["name"], call.get("input", {}))
                key = call["name"].replace("__", ".")
                outputs.append({"tool": key, "arguments": call.get("input", {}), "result": result})
                trace.append({"step": key, "message": "Model-selected read-only tool call", "arguments": call.get("input", {}), "result": result})
                results.append({"type": "tool_result", "tool_use_id": call["id"], "content": json.dumps(result), "is_error": "error" in result})
            body["messages"].append({"role": "user", "content": results})
    raise HTTPException(502, detail="Anthropic agent exceeded the four-turn tool-use limit.")


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
    elif request.method == "expanded_flat":
        t0 = time.perf_counter()
        selected = expanded_flat_retrieve(request.task, catalog)
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
            route = await route_graph(db, scenario, request.task)
            chosen_skill = route["skill"]
            selected = [next((t for t in catalog if t.key == row["key"]), None) for row in route["tools"]]
            selected = [t for t in selected if t is not None]
            resolved = route["resolved_entities"]
            paths = route["edges"]
            retrieval_ms = route["retrieval_ms"]
        except Exception as exc:
            raise as_http_error(exc) from exc

    selected_keys = {tool.key for tool in selected}
    explicit_entity_id = bool(re.search(r"\b(?:T|C|O)-[A-Za-z0-9]+\b", request.task, re.I))
    entity_lookup_miss = request.method == "graph" and explicit_entity_id and not resolved
    if entity_lookup_miss:
        result = {"success": False, "missing_tools": [], "dependency_failures": [],
            "trace": [{"step": "entity-lookup", "message": "The requested ticket/customer/order ID has no matching HydraDB entity path; no fixture data was substituted."}],
            "answer": "I could not find that ticket, customer, or order ID in HydraDB, so I did not run tools against a different record.",
            "tool_calls": 0, "tool_outputs": [], "reused_entities": []}
    elif request.execution_mode == "agent":
        if request.method != "graph":
            raise HTTPException(422, detail="Real-agent execution requires graph retrieval")
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise HTTPException(503, detail="Real-agent mode is unavailable: ANTHROPIC_API_KEY is not configured on this server")
        result = await run_anthropic_agent(request.task, scenario, selected, resolved)
    else:
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
        "execution_mode": request.execution_mode,
        "agent": result.get("agent"),
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
            recall_total = semantic_recall = graph_entity_correct = 0.0
            token_total = flat_tokens = 0
            cases: list[dict] = []
            for case in EVALUATION_CASES:
                scenario = case["scenario"]
                expected_skill = required[scenario]
                route = await route_graph(db, scenario, case["task"])
                correct += route["skill"] == expected_skill
                total += 1
                route_keys = {tool["key"] for tool in route["tools"]}
                fixture_result = execute_fixture(scenario, "graph", route_keys, route["resolved_entities"])
                entity_ok = bool(route["resolved_entities"]) == case["entity_expected"]
                graph_entity_correct += entity_ok
                has_explicit_entity = bool(re.search(r"\b(?:T|C|O)-[A-Za-z0-9]+\b", case["task"], re.I))
                graph_ok = fixture_result["success"] and entity_ok and not (has_explicit_entity and not route["resolved_entities"])
                successful += graph_ok
                needed = set(TASKS[scenario]["required"])
                if route["resolved_entities"]:
                    needed -= {"customer.resolve", "orders.list"}
                graph_recall = len(needed & route_keys) / max(1, len(needed))
                recall_total += graph_recall
                expanded = expanded_flat_retrieve(case["task"], catalog)
                flat_keys = {tool.key for tool in expanded}
                flat_result = execute_fixture(scenario, "expanded_flat", flat_keys, [])
                requested_ids = re.findall(r"\b(?:T|C|O)-[A-Za-z0-9]+\b", case["task"], re.I)
                known_ids = {TASKS[scenario]["ticket"], TASKS[scenario]["customer"], TASKS[scenario]["order"]}
                flat_success = flat_result["success"] and all(ident.upper() in {value.upper() for value in known_ids} for ident in requested_ids)
                flat_recall = len(set(TASKS[scenario]["required"]) & flat_keys) / max(1, len(TASKS[scenario]["required"]))
                semantic_recall += flat_recall
                token_total += max(1, sum(len(json.dumps(t, separators=(",", ":"))) for t in route["tools"]) // 4)
                flat_tokens += max(1, sum(len(json.dumps(tool_json(t), separators=(",", ":"))) for t in expanded) // 4)
                cases.append({"name": case["name"], "task": case["task"], "scenario": scenario,
                    "graph": {"skill_correct": route["skill"] == expected_skill, "task_success": graph_ok, "entity_context_correct": entity_ok,
                        "tool_recall": round(graph_recall, 3), "selected_tools": sorted(route_keys), "resolved_entities": route["resolved_entities"]},
                    "expanded_flat": {"task_success": flat_success, "tool_recall": round(flat_recall, 3), "selected_tools": sorted(flat_keys)},
                    "winner": "graph" if graph_ok and not flat_success else "expanded_flat" if flat_success and not graph_ok else "tie" if graph_ok == flat_success else "neither"})
            return {"accuracy": correct / total, "task_success_rate": successful / total,
                "tool_recall": recall_total / total, "context_tokens_est": token_total / total,
                "entity_context_accuracy": graph_entity_correct / total,
                "expanded_flat": {"task_success_rate": sum(c["expanded_flat"]["task_success"] for c in cases) / total,
                    "tool_recall": semantic_recall / total, "context_tokens_est": flat_tokens / total},
                "cases": cases}

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
        return {"history": history, "frozen_baseline": frozen_baseline, "training_fixtures": [row["prompt"] for row in TRAINING_TASKS.values()], "held_out": [row["task"] for row in EVALUATION_CASES], "feedback": "successful route reinforcement and training-only retrieval-miss correction", "comparison": history[-1]["cases"]}
    except Exception as exc:
        raise as_http_error(exc) from exc


@app.get("/api/graph")
async def graph(scenario: str = "refund"):
    try:
        scenario = scenario if scenario in TASKS else "refund"
        route = await route_graph(db, scenario, TASKS[scenario]["task"])
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
