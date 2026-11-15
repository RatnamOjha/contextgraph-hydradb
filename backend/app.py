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
from .config import load_env_file

load_env_file()

from .fixtures import DATA_TOOLS, EVALUATION_CASES, SKILLS, TASKS, TRAINING_TASKS, make_catalog
from .hydra import HydraClient, HydraError
from .registry import inspect_tools, seed_catalog, seed_olist_entities
from .router import candidate_skills, classify, execute_fixture, expanded_flat_retrieve, flat_candidates, flat_retrieve, route_graph, route_graph_candidates, tool_json
from .olist_store import DEFAULT_DB, available as olist_available, dataset_status, list_orders, lookup_order
from .dataset_workflow import execute_deterministic, extract_order_id
from .groq_agent import run_groq_agent

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
    real_model_cases: int = Field(default=0, ge=0, le=3)


class LearningRollbackRequest(BaseModel):
    update_id: int


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
    return {"available": bool(os.getenv("GROQ_API_KEY")) and olist_available(), "provider": "Groq", "model": os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"), "dataset_available": olist_available()}


@app.get("/api/dataset/status")
async def data_status():
    return dataset_status()


@app.get("/api/orders")
async def orders(limit: int = 50):
    try:
        return {"orders": list_orders(limit)}
    except FileNotFoundError as exc:
        raise HTTPException(503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc


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
        rows = await inspect_tools(db)
        executable = {tool.key: tool for tool in DATA_TOOLS}
        for row in rows:
            tool = executable.get(row["key"])
            row["execution_status"] = ("simulated_policy" if tool.server == "contextgraph-simulated" else "dataset_backed") if tool else "legacy_fixture_only"
            row["source"] = "ContextGraph demo policy" if tool and tool.server == "contextgraph-simulated" else "Olist historical dataset" if tool else "Synthetic legacy fixture; excluded from Groq agent tools"
            row["dependencies"] = list(tool.dependencies) if tool else []
        return {"tools": rows}
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


async def record_dataset_execution(task_text: str, method: str, success: bool, candidates: list[dict], selected: list[str],
                                   outputs: list[dict], resolved: list[dict], duration_ms: float, agent: dict | None) -> None:
    execution_id = int(time.time_ns() & ((1 << 62) - 1)) or 1
    task_id = (execution_id + 1) & ((1 << 62) - 1) or 2
    failure_rows = [row for row in outputs if row.get("error") or "error" in row.get("result", {})]
    call_errors = [row.get("result", {}).get("error", "tool error") for row in failure_rows]
    usage = agent or {}
    await db.query("UNWIND $rows AS row MERGE (e {id: row.id}) SET e:Execution, e.method=row.method, e.success=row.success, e.duration_ms=row.duration_ms, e.provider=row.provider, e.input_tokens=row.input_tokens, e.output_tokens=row.output_tokens, e.errors=row.errors",
        {"rows": [{"id": execution_id, "method": method, "success": success, "duration_ms": duration_ms, "provider": usage.get("provider", "fixture"), "input_tokens": usage.get("input_tokens", 0), "output_tokens": usage.get("output_tokens", 0), "errors": json.dumps(call_errors)}]})
    await db.query("UNWIND $rows AS row MERGE (t {id: row.id}) SET t:Task, t.prompt=row.prompt",
        {"rows": [{"id": task_id, "prompt": task_text}]})
    await db.query("UNWIND $rows AS row CREATE (e {id: row.src})-[:FOR_TASK]->(t {id: row.dst})", {"rows": [{"src": execution_id, "dst": task_id}]})
    candidate_edges = [{"src": task_id, "dst": db.id_for("Skill", row["skill"])} for row in candidates]
    if candidate_edges:
        await db.query("UNWIND $rows AS row CREATE (t {id: row.src})-[:CANDIDATE_SKILL]->(s {id: row.dst})", {"rows": candidate_edges})
    for rel, keys in (("SELECTED", selected), ("CALLED", [row["tool"] for row in outputs]), ("FAILED", [row["tool"] for row in failure_rows])):
        edges = [{"src": execution_id, "dst": db.id_for("Tool", key)} for key in keys]
        if edges:
            await db.query(f"UNWIND $rows AS row CREATE (e {{id: row.src}})-[:{rel}]->(t {{id: row.dst}})", {"rows": edges})
    resolved_edges = [{"src": execution_id, "dst": db.id_for("Entity", f"{row['type']}:{row['id']}")} for row in resolved]
    if resolved_edges:
        await db.query("UNWIND $rows AS row CREATE (e {id: row.src})-[:RESOLVED]->(n {id: row.dst})", {"rows": resolved_edges})


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


async def run_dataset_task(request: RunRequest) -> dict:
    if not olist_available():
        raise HTTPException(503, detail="Olist dataset is not installed on this server. Import the official dataset before running historical orders.")
    order_id, parse_error = extract_order_id(request.task)
    if parse_error:
        raise HTTPException(422, detail=parse_error)
    if not order_id:
        raise HTTPException(422, detail="Select a historical order from the order list and include its 32-character ID in the task.")
    source_order = lookup_order(order_id)
    if not source_order:
        started = time.perf_counter()
        try:
            await record_dataset_execution(request.task, f"{request.method}:{request.execution_mode}", False,
                candidate_skills(request.task), [], [], [], (time.perf_counter()-started)*1000, None)
        except Exception:
            log.exception("unknown_order_trace_write_failed")
        return {"scenario": "olist_order", "task": request.task, "method": request.method,
            "execution_mode": request.execution_mode, "selected_tools": [], "paths": [], "resolved_entities": [],
            "metrics": {"task_success": False, "tool_precision": 0, "tool_recall": 0, "context_tokens_est": 0,
                "retrieval_ms": 0, "end_to_end_ms": 0, "unnecessary_tools": 0, "tool_calls": 0,
                "dependency_success": False, "entity_resolution_correct": False},
            "trace": [{"step": "entity-lookup", "message": f"No Olist order exists for ID {order_id}; no tool calls were run."}],
            "tool_outputs": [], "answer": f"Unknown Olist order ID: {order_id}. Choose an order from the imported sample."}

    candidates = candidate_skills(request.task)
    started = time.perf_counter()
    selected: list = []
    paths: list = []
    resolved: list = []
    retrieval_ms = 0.0
    if request.method == "graph":
        route = await route_graph_candidates(db, candidates, order_id)
        by_key = {tool.key: tool for tool in DATA_TOOLS}
        for row in route["tools"]:
            tool = by_key.get(row["key"])
            if tool:
                selected.append(tool)
                row["why"] = row.get("why", [])
        # Attach structured relationship evidence to the selected tool metadata.
        reasons = {row["key"]: row.get("why", []) for row in route["tools"]}
        paths, resolved, retrieval_ms = route["edges"], route["resolved_entities"], route["retrieval_ms"]
    elif request.method == "full":
        selected = list(DATA_TOOLS)
        reasons = {tool.key: ["Included by full-context baseline"] for tool in selected}
    else:
        selected = flat_candidates(request.task, candidates, expand=request.method == "expanded_flat")
        reasons = {tool.key: ["Local skill candidate plus lexical ranking/dependency metadata"] for tool in selected}
        retrieval_ms = (time.perf_counter() - started) * 1000
    if request.method == "graph" and not selected:
        raise HTTPException(503, detail="HydraDB returned no executable tools. Seed the catalog and Olist entity graph first.")
    if request.method == "graph" and not resolved:
        # The selected order was verified in SQLite. Require its graph node too;
        # never imply a HydraDB entity resolution if the graph is stale.
        raise HTTPException(503, detail="The order exists in SQLite but its HydraDB entity path is missing. Re-seed the context graph.")

    context_json = json.dumps([tool_json(tool) for tool in selected], separators=(",", ":"))
    context_token_est = max(1, len(context_json)//4)
    context_budget = max(300, min(12000, int(os.getenv("CONTEXTGRAPH_TOOL_CONTEXT_TOKEN_BUDGET", "2500"))))
    if context_token_est > context_budget:
        raise HTTPException(413, detail=f"Retrieved context ({context_token_est} estimated tokens) exceeds the configured budget ({context_budget}); required dependencies were not dropped.")
    agent_result = await run_groq_agent(request.task, selected, DEFAULT_DB, order_id) if request.execution_mode == "agent" else execute_deterministic(request.task, order_id, selected, DEFAULT_DB)
    agent_result["trace"].insert(0, {"step": "task-analysis", "message": "Candidate skills: " + ", ".join(row["skill"] for row in candidates)})
    end_ms = (time.perf_counter() - started) * 1000
    try:
        await record_dataset_execution(request.task, f"{request.method}:{request.execution_mode}", agent_result["success"], candidates,
            [tool.key for tool in selected], agent_result["tool_outputs"], resolved, end_ms, agent_result.get("agent"))
    except Exception as exc:
        log.exception("dataset_execution_trace_write_failed")
        agent_result["trace"].append({"step": "trace-warning", "message": f"Could not persist execution evidence: {exc}"})
    selected_json = []
    for tool in selected:
        row = tool_json(tool)
        row["why"] = reasons.get(tool.key, [])
        row["execution_status"] = "simulated_policy" if tool.server == "contextgraph-simulated" else "dataset_backed"
        row["source"] = "ContextGraph demo policy" if tool.server == "contextgraph-simulated" else "Olist historical dataset"
        selected_json.append(row)
    expected = set(__import__("backend.dataset_workflow", fromlist=["required_tools"]).required_tools(request.task))
    succeeded = {row["tool"] for row in agent_result["tool_outputs"] if not row.get("error")}
    usage = agent_result.get("agent") or {}
    return {"scenario": "olist_order", "task": request.task, "method": request.method,
        "execution_mode": request.execution_mode, "agent": usage, "skill": [row["skill"] for row in candidates],
        "selected_tools": selected_json, "paths": paths, "resolved_entities": resolved,
        "metrics": {"task_success": agent_result["success"], "tool_precision": round(len(expected & {t.key for t in selected}) / max(1, len(selected)), 3),
            "tool_recall": round(len(expected & {t.key for t in selected}) / max(1, len(expected)), 3),
            "context_tokens_est": context_token_est, "retrieval_ms": round(retrieval_ms, 3),
            "end_to_end_ms": round(end_ms, 3), "unnecessary_tools": len({t.key for t in selected} - expected),
            "tool_calls": agent_result["tool_calls"], "dependency_success": not agent_result["missing_tools"] and not agent_result["dependency_failures"],
            "argument_correctness": not any(row.get("error") for row in agent_result["tool_outputs"]),
            "entity_resolution_correct": any(row["type"] == "order" and row["id"] == order_id for row in resolved),
            "actual_input_tokens": usage.get("input_tokens"), "actual_output_tokens": usage.get("output_tokens")},
        "trace": agent_result["trace"], "tool_outputs": agent_result["tool_outputs"], "answer": agent_result["answer"]}


@app.post("/api/run")
async def run_task(request: RunRequest):
    if request.execution_mode == "agent" or re.search(r"\b[0-9a-f]{32}\b", request.task, re.I):
        if request.execution_mode == "agent" and request.method not in {"graph", "flat", "expanded_flat", "full"}:
            raise HTTPException(422, detail="Dataset-backed agent mode supports graph, flat, expanded_flat, or full retrieval.")
        return await run_dataset_task(request)
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
    if olist_available():
        from .evaluation import evaluate_olist
        try:
            result = await evaluate_olist(db)
            if request.real_model_cases:
                if not os.getenv("GROQ_API_KEY"):
                    raise HTTPException(503, detail="Set GROQ_API_KEY on the API server to run the optional real-model subset.")
                model_results = []
                for case in result["cases"][:request.real_model_cases]:
                    candidates = candidate_skills(case["task"])
                    route = await route_graph_candidates(db, candidates, case["order_id"])
                    allowed = {row["key"] for row in route["tools"]}
                    tools = [tool for tool in DATA_TOOLS if tool.key in allowed]
                    output = await run_groq_agent(case["task"], tools, DEFAULT_DB, case["order_id"])
                    model_results.append({"case": case["name"], "success": output["success"], "answer": output["answer"], "agent": output.get("agent"), "tool_calls": output["tool_outputs"]})
                result["real_agent_subset"] = model_results
                result["real_model_case_count"] = len(model_results)
                result["real_model_provider_calls"] = sum(row["agent"]["provider_calls"] for row in model_results if row.get("agent"))
            return result
        except HTTPException:
            raise
        except Exception as exc:
            raise as_http_error(exc) from exc
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


async def set_dependency_weight(tool_key: str, dependency_key: str, weight: float) -> None:
    tool_id = db.id_for("Tool", tool_key)
    dependency_id = db.id_for("Tool", dependency_key)
    await db.query(f"MATCH (a:Tool {{id: {tool_id}}})-[r:DEPENDS_ON]->(b:Tool {{id: {dependency_id}}}) SET r.weight = {weight:.1f}")


@app.post("/api/learning/demo")
async def learning_demo():
    """Remove a prerequisite, observe the failure, repair from evidence, and retest on a new order."""
    if not olist_available(): raise HTTPException(503, detail="Import the Olist demo subset first.")
    rows = list_orders(20)
    if len(rows) < 2: raise HTTPException(503, detail="The learning demo requires two distinct source orders.")
    training, heldout = rows[0]["order_id"], rows[1]["order_id"]
    tool_key, dependency = "order.items", "order.details"
    train_task = f"Identify the line-item goods and seller IDs for historical record {training}."
    test_task = f"Which purchased goods were included in source record {heldout}?"
    candidates = candidate_skills(train_task)
    await set_dependency_weight(tool_key, dependency, 0.0)
    applied = False
    try:
        before_route = await route_graph_candidates(db, candidates, training)
        by_key = {tool.key: tool for tool in DATA_TOOLS}
        before_tools = [by_key[row["key"]] for row in before_route["tools"] if row["key"] in by_key]
        before = execute_deterministic(train_task, training, before_tools, DEFAULT_DB)
        await record_dataset_execution(train_task, "graph:fixture:dependency-removed", before["success"], candidates,
            [tool.key for tool in before_tools], before["tool_outputs"], before_route["resolved_entities"], 0.0, None)
        evidence = before["dependency_failures"]
        proposed = next(((row["tool"], row["missing_dependency"]) for row in evidence
                         if row["tool"] == tool_key and row["missing_dependency"] == dependency), None)
        declared = next((tool for tool in DATA_TOOLS if tool.key == tool_key), None)
        if not proposed or not declared or dependency not in declared.dependencies:
            raise HTTPException(409, detail={"message": "Execution evidence did not validate the missing dependency; no graph update was applied.", "evidence": evidence})

        update_id = int(time.time_ns() & ((1 << 62) - 1)) or 3
        await set_dependency_weight(tool_key, dependency, 1.0)
        applied = True
        await db.query("UNWIND $rows AS row MERGE (u {id: row.id}) SET u:LearningUpdate, u.tool=row.tool, u.dependency=row.dependency, u.previous_weight=row.previous_weight, u.applied_weight=row.applied_weight, u.state=row.state, u.evidence=row.evidence, u.training_order=row.training_order, u.heldout_order=row.heldout_order",
            {"rows": [{"id": update_id, "tool": tool_key, "dependency": dependency, "previous_weight": 0.0,
                "applied_weight": 1.0, "state": "applied", "evidence": json.dumps(evidence), "training_order": training, "heldout_order": heldout}]})
        after_candidates = candidate_skills(test_task)
        after_route = await route_graph_candidates(db, after_candidates, heldout)
        after_tools = [by_key[row["key"]] for row in after_route["tools"] if row["key"] in by_key]
        after = execute_deterministic(test_task, heldout, after_tools, DEFAULT_DB)
        await record_dataset_execution(test_task, "graph:fixture:after-learning", after["success"], after_candidates,
            [tool.key for tool in after_tools], after["tool_outputs"], after_route["resolved_entities"], 0.0, None)
        return {"update_id": update_id, "training_order_id": training, "heldout_order_id": heldout,
            "before": {"task": train_task, "success": before["success"], "selected_tools": [t.key for t in before_tools], "trace": before["trace"], "dependency_failures": evidence},
            "proposal": {"tool": tool_key, "missing_dependency": dependency, "evidence": evidence, "validation": "matched declared tool prerequisite and observed execution dependency failure"},
            "mutation": {"edge": f"{tool_key} -[DEPENDS_ON]-> {dependency}", "weight_before": 0.0, "weight_after": 1.0, "state": "applied"},
            "after": {"task": test_task, "success": after["success"], "selected_tools": [t.key for t in after_tools], "trace": after["trace"], "dependency_failures": after["dependency_failures"]},
            "heldout_is_disjoint": training != heldout, "rollback_endpoint": f"/api/learning/rollback/{update_id}"}
    except Exception:
        if not applied:
            await set_dependency_weight(tool_key, dependency, 1.0)
        raise


@app.post("/api/learning/rollback/{update_id}")
async def rollback_learning(update_id: int):
    try:
        _, rows, _ = await db.query(f"MATCH (u:LearningUpdate {{id: {update_id}}}) RETURN u.tool AS tool,u.dependency AS dependency,u.previous_weight AS previous_weight,u.state AS state")
        if not rows: raise HTTPException(404, detail="Learning update was not found.")
        tool, dependency, previous, state = rows[0]
        if state != "applied": raise HTTPException(409, detail=f"Learning update is already {state}.")
        await set_dependency_weight(tool, dependency, float(previous))
        await db.query(f"MATCH (u:LearningUpdate {{id: {update_id}}}) SET u.state = 'rolled_back'")
        return {"update_id": update_id, "state": "rolled_back", "edge": f"{tool} -[DEPENDS_ON]-> {dependency}", "restored_weight": previous}
    except HTTPException:
        raise
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
