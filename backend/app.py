from __future__ import annotations

import json
import logging
import os
import re
import time
import asyncio
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
from .objectives import evaluate_answer, requested_objectives
from .real_evaluation import compare_real_agent

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("contextgraph")
db = HydraClient()
_learning_lock = asyncio.Lock()
_agent_eval_lock = asyncio.Lock()


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


class AgentEvalRequest(BaseModel):
    cases: int = Field(default=1, ge=1, le=3)
    max_provider_requests: int = Field(default=20, ge=1, le=30)
    max_total_tokens: int = Field(default=40000, ge=512, le=60000)
    max_tool_calls: int = Field(default=20, ge=1, le=60)
    resume: bool = True


class LearningRollbackRequest(BaseModel):
    update_id: int


@asynccontextmanager
async def lifespan(_: FastAPI):
    await db.open()
    await recover_interrupted_learning_experiments()
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
    configured = bool(os.getenv("GROQ_API_KEY"))
    dataset = olist_available()
    return {"available": configured and dataset, "configured": configured, "provider": "Groq",
        "model": os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"), "dataset_available": dataset,
        "provider_call_status": "not_probed"}


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
            "outcome": {"code": "BLOCKED", "explanation": "The requested Olist order ID does not resolve to a source record.", "verified_objectives": 0,
                "total_objectives": len(requested_objectives(request.task)), "objectives": [], "unsupported_claims": [], "evidence": {}, "structured_summary": {}},
            "execution": {"tools_exposed": [], "tools_called": [], "invalid_calls": 0, "repeated_calls": 0, "execution_failures": 0, "iterations": 0},
            "retrieval": {"relevant_tool_coverage": {}, "dependency_complete": False, "entity_resolved": False, "context_tokens_est": 0, "retrieval_ms": 0, "objective_count": len(requested_objectives(request.task))},
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
    agent_error = None
    if request.execution_mode == "agent":
        try:
            agent_result = await run_groq_agent(request.task, selected, DEFAULT_DB, order_id)
        except HTTPException as exc:
            provider_details = getattr(exc, "provider_error", None)
            agent_error = {"status_code": exc.status_code, "message": str(exc.detail),
                "provider_error": provider_details}
            agent_result = {"success": False, "answer": "Groq did not complete this request. No fixture fallback was run; factual objectives remain unverified.",
                "tool_calls": 0, "tool_outputs": [], "trace": [{"step": "groq-provider-failure", "message": str(exc.detail)}],
                "missing_tools": [], "dependency_failures": [], "reused_entities": [],
                "execution": {"tools_exposed": sorted(tool.key for tool in selected), "tools_called": [], "invalid_calls": 0,
                    "repeated_calls": 0, "execution_failures": 0, "iterations": 0, "provider_error": agent_error}}
    else:
        agent_result = execute_deterministic(request.task, order_id, selected, DEFAULT_DB)
    entity_match = any(row["type"] == "order" and row["id"] == order_id for row in resolved) if request.method == "graph" else lookup_order(order_id) is not None
    outcome = evaluate_answer(request.task, agent_result["answer"], agent_result["tool_outputs"], entity_resolved=entity_match)
    agent_result["outcome"] = outcome
    agent_result["success"] = outcome["code"] == "SUCCESS"
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
        row["dependencies"] = list(tool.dependencies)
        row["execution_status"] = "simulated_policy" if tool.server == "contextgraph-simulated" else "dataset_backed"
        row["source"] = "ContextGraph demo policy" if tool.server == "contextgraph-simulated" else "Olist historical dataset"
        selected_json.append(row)
    expected = set(__import__("backend.dataset_workflow", fromlist=["required_tools"]).required_tools(request.task))
    objective_providers = {"status": {"order.details", "order.delivery_status"}, "payment_total": {"order.payments"},
        "payment_methods": {"order.payments"}, "installments": {"order.payments"}, "delivery_timing": {"order.delivery_status"},
        "items": {"order.items"}, "seller_ids": {"order.items"}, "customer_orders": {"customer.orders"}, "purchase_date": {"order.details"},
        "item_total": {"order.details"}, "refund_eligibility": {"refund.eligibility"}}
    objective_rows = requested_objectives(request.task)
    selected_keys = {tool.key for tool in selected}
    relevant = {obj["id"]: sorted(objective_providers.get(obj["id"], set()) & selected_keys) for obj in objective_rows}
    dependency_complete = all(set(tool.dependencies).issubset(selected_keys) for tool in selected)
    execution = agent_result.get("execution") or {"tools_exposed": sorted(selected_keys),
        "tools_called": sorted({row["tool"] for row in agent_result["tool_outputs"]}),
        "tools_executed": sorted({row["tool"] for row in agent_result["tool_outputs"] if not row.get("error")}),
        "invalid_calls": sum(1 for row in agent_result["tool_outputs"] if row.get("error")),
        "repeated_calls": 0, "execution_failures": sum(1 for row in agent_result["tool_outputs"] if row.get("error")), "iterations": 0}
    usage = agent_result.get("agent") or {}
    return {"scenario": "olist_order", "task": request.task, "method": request.method,
        "execution_mode": request.execution_mode, "agent": usage, "agent_error": agent_error, "skill": [row["skill"] for row in candidates],
        "selected_tools": selected_json, "paths": paths, "resolved_entities": resolved,
        "outcome": outcome, "execution": execution,
        "retrieval": {"relevant_tool_coverage": relevant, "dependency_complete": dependency_complete,
            "entity_resolved": entity_match, "context_tokens_est": context_token_est,
            "retrieval_ms": round(retrieval_ms, 3), "objective_count": len(objective_rows)},
        "metrics": {"task_success": agent_result["success"], "tool_precision": round(len(expected & {t.key for t in selected}) / max(1, len(selected)), 3),
            "tool_recall": round(len(expected & {t.key for t in selected}) / max(1, len(expected)), 3),
            "context_tokens_est": context_token_est, "retrieval_ms": round(retrieval_ms, 3),
            "end_to_end_ms": round(end_ms, 3), "unnecessary_tools": len({t.key for t in selected} - expected),
            "tool_calls": agent_result["tool_calls"], "dependency_success": not agent_result["missing_tools"] and not agent_result["dependency_failures"],
            "argument_correctness": not any(row.get("error") for row in agent_result["tool_outputs"]),
            "entity_resolution_correct": entity_match,
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


@app.post("/api/evaluate/agent")
async def evaluate_agent(request: AgentEvalRequest):
    """Run a sequential, budgeted same-model comparison; resume from HydraDB checkpoints."""
    if not os.getenv("GROQ_API_KEY"):
        raise HTTPException(503, detail="Set GROQ_API_KEY on the API server to run this real-agent pilot.")
    async with _agent_eval_lock:
        try:
            return await compare_real_agent(db, case_count=request.cases,
                max_provider_requests=request.max_provider_requests, max_total_tokens=request.max_total_tokens,
                max_tool_calls=request.max_tool_calls, resume=request.resume)
        except Exception as exc:
            raise as_http_error(exc) from exc


def dependency_edge_id(tool_key: str, dependency_key: str) -> str:
    return f"Tool/{db.id_for('Tool', tool_key)}-[DEPENDS_ON]->Tool/{db.id_for('Tool', dependency_key)}"


async def get_dependency_state(tool_key: str, dependency_key: str) -> dict:
    tool_id = db.id_for("Tool", tool_key)
    dependency_id = db.id_for("Tool", dependency_key)
    _, rows, _ = await db.query(f"MATCH (a:Tool {{id: {tool_id}}})-[r:DEPENDS_ON]->(b:Tool {{id: {dependency_id}}}) RETURN r.weight AS weight")
    if not rows:
        raise HTTPException(409, detail=f"Expected dependency edge {tool_key} → {dependency_key} is absent; the demo will not create or delete it.")
    if len(rows) != 1:
        raise HTTPException(409, detail=f"Expected one dependency edge for {tool_key} → {dependency_key}, found {len(rows)}; refusing an ambiguous mutation.")
    if rows[0][0] is None:
        raise HTTPException(409, detail="The dependency edge has no numeric weight; exact snapshot restoration is not supported for this edge state.")
    weight = float(rows[0][0])
    return {"edge_exists": True, "weight": weight, "properties": {"weight": weight}}


async def set_dependency_weight(tool_key: str, dependency_key: str, weight: float) -> None:
    if not isinstance(weight, (float, int)):
        raise ValueError("Dependency weight must be numeric.")
    tool_id = db.id_for("Tool", tool_key)
    dependency_id = db.id_for("Tool", dependency_key)
    await db.query(f"MATCH (a:Tool {{id: {tool_id}}})-[r:DEPENDS_ON]->(b:Tool {{id: {dependency_id}}}) SET r.weight = {float(weight):.17g}")


async def restore_dependency_state(tool_key: str, dependency_key: str, snapshot: dict) -> dict:
    if not snapshot.get("edge_exists"):
        raise HTTPException(409, detail="The original dependency edge was absent; restoration would require creating a relationship, which this demo does not do.")
    await set_dependency_weight(tool_key, dependency_key, float(snapshot["weight"]))
    restored = await get_dependency_state(tool_key, dependency_key)
    if restored != snapshot:
        raise HTTPException(409, detail=f"Dependency edge restoration did not match its snapshot: expected {snapshot}, observed {restored}.")
    return restored


async def create_learning_experiment(update_id: int, tool: str, dependency: str, learning_mode: str,
                                     training: str, heldout: str, snapshot: dict) -> None:
    snapshot_json = json.dumps(snapshot, separators=(",", ":"))
    experiment_state = {**snapshot, "retrieval_exclusion": {"from": tool, "relationship_type": "DEPENDS_ON", "to": dependency}}
    await db.query("UNWIND $rows AS row MERGE (u {id: row.id}) SET u:LearningUpdate, u.tool=row.tool, u.dependency=row.dependency, u.learning_mode=row.learning_mode, u.training_order=row.training_order, u.heldout_order=row.heldout_order, u.edge_identifier=row.edge_identifier, u.relationship_type='DEPENDS_ON', u.previous_weight=row.previous_weight, u.applied_weight=row.previous_weight, u.pre_state=row.pre_state, u.experiment_state=row.experiment_state, u.state='experimenting', u.evidence='{}'",
        {"rows": [{"id": update_id, "tool": tool, "dependency": dependency, "learning_mode": learning_mode,
            "training_order": training, "heldout_order": heldout, "edge_identifier": dependency_edge_id(tool, dependency),
            "previous_weight": snapshot["weight"],
            "pre_state": snapshot_json, "experiment_state": json.dumps(experiment_state, separators=(",", ":"))}]})


async def finish_learning_experiment(update_id: int, *, state: str, evidence: dict | list,
                                     diagnosis: str, post_state: dict | None = None,
                                     rollback_state: dict | None = None) -> None:
    await db.query(f"MATCH (u:LearningUpdate {{id: {update_id}}}) SET u.state = $state, u.evidence = $evidence, u.diagnosis = $diagnosis, u.post_state = $post_state, u.rollback_state = $rollback_state, u.finished_at = $finished_at",
        {"state": state, "evidence": json.dumps(evidence, ensure_ascii=False), "diagnosis": diagnosis,
            "post_state": json.dumps(post_state, separators=(",", ":")) if post_state is not None else None,
            "rollback_state": json.dumps(rollback_state, separators=(",", ":")) if rollback_state is not None else None,
            "finished_at": time.time()})


async def recover_interrupted_learning_experiments() -> None:
    _, rows, _ = await db.query("MATCH (u:LearningUpdate {state: 'experimenting'}) RETURN u.id AS id,u.tool AS tool,u.dependency AS dependency,u.pre_state AS pre_state,u.experiment_state AS experiment_state")
    for update_id, tool, dependency, raw_snapshot, raw_experiment in rows:
        if not tool or not dependency or not raw_snapshot:
            raise RuntimeError(f"Learning experiment {update_id} lacks a recovery snapshot; refusing to serve with unknown graph state.")
        snapshot = json.loads(raw_snapshot)
        experiment = json.loads(raw_experiment) if raw_experiment else {}
        current = await get_dependency_state(tool, dependency)
        if "retrieval_exclusion" in experiment:
            if current != snapshot:
                raise RuntimeError(f"Retrieval-only learning experiment {update_id} found graph drift; refusing automatic mutation.")
            restored = current
        else:
            # Recover pre-filter experiments that used the older weight mutation.
            restored = await restore_dependency_state(tool, dependency, snapshot)
        await db.query(f"MATCH (u:LearningUpdate {{id: {int(update_id)}}}) SET u.state = $state, u.recovered_at = $recovered_at, u.rollback_state = $rollback_state",
            {"state": "interrupted_restored", "recovered_at": time.time(), "rollback_state": json.dumps(restored, separators=(",", ":"))})
        log.warning("Recovered interrupted graph experiment %s for %s -> %s", update_id, tool, dependency)


@app.post("/api/learning/demo")
async def learning_demo():
    """Withhold a prerequisite from one retrieval through an explicit filter; keep the shared edge unchanged."""
    if not olist_available(): raise HTTPException(503, detail="Import the Olist demo subset first.")
    rows = list_orders(20)
    if len(rows) < 2: raise HTTPException(503, detail="The learning demo requires two distinct source orders.")
    async with _learning_lock:
        training, heldout = rows[0]["order_id"], rows[1]["order_id"]
        tool_key, dependency = "order.items", "order.details"
        train_task = f"Identify the line-item goods and seller IDs for historical record {training}."
        test_task = f"Which purchased goods were included in source record {heldout}?"
        candidates = candidate_skills(train_task)
        baseline = await get_dependency_state(tool_key, dependency)
        if baseline["weight"] <= 0:
            raise HTTPException(409, detail="The dependency is already inactive; the fixture experiment requires an active baseline edge.")
        update_id = int(time.time_ns() & ((1 << 62) - 1)) or 3
        await create_learning_experiment(update_id, tool_key, dependency, "fixture", training, heldout, baseline)
        correction_applied = False
        finished = False
        try:
            experiment_state = {**baseline, "retrieval_exclusion": {"from": tool_key, "relationship_type": "DEPENDS_ON", "to": dependency}}
            before_route = await route_graph_candidates(db, candidates, training, exclude_dependencies={(tool_key, dependency)})
            by_key = {tool.key: tool for tool in DATA_TOOLS}
            before_tools = [by_key[row["key"]] for row in before_route["tools"] if row["key"] in by_key]
            before = execute_deterministic(train_task, training, before_tools, DEFAULT_DB)
            await record_dataset_execution(train_task, "graph:fixture:dependency-withheld", before["success"], candidates,
                [tool.key for tool in before_tools], before["tool_outputs"], before_route["resolved_entities"], 0.0, None)
            evidence = before["dependency_failures"]
            declared = next((tool for tool in DATA_TOOLS if tool.key == tool_key), None)
            if not any(row.get("tool") == tool_key and row.get("missing_dependency") == dependency for row in evidence) or not declared or dependency not in declared.dependencies:
                restored = await get_dependency_state(tool_key, dependency)
                await finish_learning_experiment(update_id, state="no_update", evidence=evidence,
                    diagnosis="No validated dependency evidence", post_state=experiment_state, rollback_state=restored)
                finished = True
                return {"state": "no_update", "mode": "fixture", "diagnosis": "No validated evidence; the dependency was restored to baseline.",
                    "evidence": evidence, "graph_states": {"baseline": baseline, "experiment": experiment_state, "rollback": restored}}

            corrected_state = await get_dependency_state(tool_key, dependency)
            if corrected_state != baseline:
                raise HTTPException(409, detail="HydraDB did not confirm restoration of the dependency's baseline state.")
            await finish_learning_experiment(update_id, state="applied", evidence=evidence,
                diagnosis="dependency_withheld_from_retrieval_then_filter_removed", post_state=corrected_state)
            correction_applied = True
            after_candidates = candidate_skills(test_task)
            after_route = await route_graph_candidates(db, after_candidates, heldout)
            after_tools = [by_key[row["key"]] for row in after_route["tools"] if row["key"] in by_key]
            after = execute_deterministic(test_task, heldout, after_tools, DEFAULT_DB)
            await record_dataset_execution(test_task, "graph:fixture:after-learning", after["success"], after_candidates,
                [tool.key for tool in after_tools], after["tool_outputs"], after_route["resolved_entities"], 0.0, None)
            finished = True
            return {"state": "applied", "update_id": update_id, "mode": "fixture", "training_order_id": training, "heldout_order_id": heldout,
                "before": {"task": train_task, "success": before["success"], "selected_tools": [t.key for t in before_tools], "trace": before["trace"], "dependency_failures": evidence},
                "proposal": {"tool": tool_key, "relationship": "DEPENDS_ON", "dependency": dependency, "evidence": evidence,
                    "validation": "matched declared tool prerequisite and observed fixture execution dependency failure"},
                "mutation": {"id": update_id, "edge_identifier": dependency_edge_id(tool_key, dependency),
                    "edge": f"{tool_key} -[DEPENDS_ON]-> {dependency}", "relationship_type": "DEPENDS_ON",
                    "properties_before": baseline["properties"], "properties_experiment": experiment_state["properties"],
                    "properties_after": corrected_state["properties"], "state": "applied",
                    "semantics": "Existing edge stayed unchanged in HydraDB. The experiment passed an explicit retrieval filter that withheld this edge for one route; correction removed that filter."},
                "graph_states": {"baseline": baseline, "experiment": experiment_state, "after_correction": corrected_state},
                "after": {"task": test_task, "success": after["success"], "selected_tools": [t.key for t in after_tools], "trace": after["trace"], "dependency_failures": after["dependency_failures"]},
                "heldout_is_disjoint": training != heldout, "rollback_endpoint": f"/api/learning/rollback/{update_id}"}
        finally:
            if not correction_applied:
                restored = await get_dependency_state(tool_key, dependency)
                if not finished:
                    await finish_learning_experiment(update_id, state="interrupted_restored", evidence=[],
                        diagnosis="fixture_experiment_did_not_complete", rollback_state=restored)


@app.post("/api/learning/agent-demo")
async def agent_learning_demo():
    """Use a real Groq run's observed dependency rejection as evidence to correct retrieval filtering."""
    if not os.getenv("GROQ_API_KEY"):
        raise HTTPException(503, detail="Real-agent learning requires GROQ_API_KEY on this server; no fixture fallback is used.")
    if not olist_available(): raise HTTPException(503, detail="Import the Olist demo subset first.")
    rows = list_orders(20)
    if len(rows) < 2: raise HTTPException(503, detail="Real-agent learning requires two distinct Olist orders.")
    async with _learning_lock:
        training, heldout = rows[0]["order_id"], rows[1]["order_id"]
        tool_key, dependency = "order.items", "order.details"
        train_task = f"List line items, categories, and seller IDs for reference {training}."
        test_task = f"Identify the goods and product categories for historical record {heldout}."
        candidates = candidate_skills(train_task)
        baseline = await get_dependency_state(tool_key, dependency)
        if baseline["weight"] <= 0:
            raise HTTPException(409, detail="The dependency is already inactive; the real-agent experiment requires an active baseline edge.")
        declared = next((tool for tool in DATA_TOOLS if tool.key == tool_key), None)
        if not declared or dependency not in declared.dependencies:
            raise HTTPException(409, detail="The catalog no longer declares the dependency required for this controlled demonstration.")
        update_id = int(time.time_ns() & ((1 << 62) - 1)) or 3
        await create_learning_experiment(update_id, tool_key, dependency, "real_groq", training, heldout, baseline)
        correction_applied = False
        finished = False
        experiment_state = None
        try:
            experiment_state = {**baseline, "retrieval_exclusion": {"from": tool_key, "relationship_type": "DEPENDS_ON", "to": dependency}}
            before_route = await route_graph_candidates(db, candidates, training, exclude_dependencies={(tool_key, dependency)})
            by_key = {tool.key: tool for tool in DATA_TOOLS}
            before_keys = {row["key"] for row in before_route["tools"] if row["key"] in by_key}
            before_tools = [by_key[key] for key in sorted(before_keys)]
            try:
                before = await run_groq_agent(train_task, before_tools, DEFAULT_DB, training,
                    provider_request_budget=3, tool_call_budget=4)
            except HTTPException as exc:
                restored = await get_dependency_state(tool_key, dependency)
                await finish_learning_experiment(update_id, state="provider_failure", evidence=[],
                    diagnosis=str(getattr(exc, "provider_error", {}).get("kind", "provider_error")),
                    post_state=experiment_state, rollback_state=restored)
                finished = True
                return {"state": "provider_failure", "mode": "real_groq", "message": exc.detail,
                    "provider_error": getattr(exc, "provider_error", None), "graph_restored": True,
                    "mutation_applied": False, "training_order_id": training,
                    "retrieved_tools": sorted(before_keys), "retrieval_ms": before_route["retrieval_ms"],
                    "graph_states": {"baseline": baseline, "experiment": experiment_state, "rollback": restored},
                    "mutation": {"id": update_id, "edge_identifier": dependency_edge_id(tool_key, dependency),
                        "relationship_type": "DEPENDS_ON", "properties_before": baseline["properties"],
                        "properties_experiment": experiment_state["properties"], "properties_after": None}}
            blocked = []
            for row in before["tool_outputs"]:
                message = str(row.get("result", {}).get("error", ""))
                tool = by_key.get(row.get("tool"))
                if dependency in message and tool and dependency in tool.dependencies and dependency not in before_keys:
                    blocked.append({"tool": tool.key, "missing_dependency": dependency,
                        "observed_error": message, "call_arguments": row.get("arguments"),
                        "evidence_source": "real Groq-selected tool call rejected by runtime dependency validation"})
            await record_dataset_execution(train_task, "graph:real-agent-learning:before", before["success"], candidates,
                sorted(before_keys), before["tool_outputs"], before_route["resolved_entities"], before_route["retrieval_ms"], before.get("agent"))
            diagnosis = "missing_dependency" if blocked else ("dataset_limitation" if before["outcome"]["total_objectives"] and all(not o["evidence_sufficient"] for o in before["outcome"]["objectives"]) else ("model_reasoning_failure" if before["tool_calls"] == 0 else "execution_or_answer_failure"))
            if before["outcome"]["code"] == "SUCCESS":
                diagnosis = "valid_alternative_path"
            if not blocked:
                restored = await get_dependency_state(tool_key, dependency)
                await finish_learning_experiment(update_id, state="no_update", evidence=before["tool_outputs"],
                    diagnosis=diagnosis, post_state=experiment_state, rollback_state=restored)
                finished = True
                return {"state": "no_update", "mode": "real_groq", "diagnosis": diagnosis,
                    "explanation": "The real run did not produce validated evidence that this prerequisite is missing; no graph mutation was made.",
                    "training_order_id": training, "before": before, "retrieval": {"tools": sorted(before_keys),
                        "edges": before_route["edges"], "resolved_entities": before_route["resolved_entities"], "retrieval_ms": before_route["retrieval_ms"]},
                    "graph_restored": True, "graph_states": {"baseline": baseline, "experiment": experiment_state, "rollback": restored},
                    "mutation": {"id": update_id, "edge_identifier": dependency_edge_id(tool_key, dependency),
                        "relationship_type": "DEPENDS_ON", "properties_before": baseline["properties"],
                        "properties_experiment": experiment_state["properties"], "properties_after": None}}

            corrected_state = await get_dependency_state(tool_key, dependency)
            if corrected_state != baseline:
                raise HTTPException(409, detail="HydraDB did not confirm restoration of the dependency's baseline state.")
            evidence = {"training_task": train_task, "training_order_id": training,
                "retrieved_tools_before": sorted(before_keys), "blocked_execution_calls": blocked,
                "agent_outcome": before["outcome"], "provider": before.get("agent"),
                "validation": "observed a real model-selected call rejected because a schema-declared prerequisite was absent from the HydraDB retrieval"}
            await finish_learning_experiment(update_id, state="applied", evidence=evidence,
                diagnosis="dependency_withheld_from_retrieval_then_filter_removed", post_state=corrected_state)
            correction_applied = True
            after_candidates = candidate_skills(test_task)
            after_route = await route_graph_candidates(db, after_candidates, heldout)
            after_keys = {row["key"] for row in after_route["tools"] if row["key"] in by_key}
            after_tools = [by_key[key] for key in sorted(after_keys)]
            try:
                after = await run_groq_agent(test_task, after_tools, DEFAULT_DB, heldout,
                    provider_request_budget=5, tool_call_budget=4)
            except HTTPException as exc:
                after = {"success": False, "answer": "Held-out Groq execution did not complete: " + str(exc.detail),
                    "tool_calls": 0, "tool_outputs": [], "trace": [{"step": "provider-failure", "message": str(exc.detail)}],
                    "outcome": {"code": "UNVERIFIED", "explanation": "Provider failure prevented held-out verification.",
                        "verified_objectives": 0, "total_objectives": len(requested_objectives(test_task)),
                        "objectives": [], "unsupported_claims": [], "evidence": {}, "structured_summary": {}},
                    "execution": {"tools_exposed": sorted(after_keys), "tools_called": [], "invalid_calls": 0,
                        "repeated_calls": 0, "execution_failures": 0, "iterations": 0}}
            after_outcome = evaluate_answer(test_task, after["answer"], after["tool_outputs"],
                entity_resolved=any(x["type"] == "order" and x["id"] == heldout for x in after_route["resolved_entities"]))
            after["outcome"] = after_outcome
            after["success"] = after_outcome["code"] == "SUCCESS"
            await record_dataset_execution(test_task, "graph:real-agent-learning:after", after["success"], after_candidates,
                sorted(after_keys), after["tool_outputs"], after_route["resolved_entities"], after_route["retrieval_ms"], after.get("agent"))
            before_context = max(1, sum(len(json.dumps(tool_json(t), separators=(",", ":"))) for t in before_tools)//4)
            after_context = max(1, sum(len(json.dumps(tool_json(t), separators=(",", ":"))) for t in after_tools)//4)
            finished = True
            return {"state": "applied", "mode": "real_groq", "update_id": update_id,
                "training_order_id": training, "heldout_order_id": heldout, "heldout_is_disjoint": training != heldout,
                "training_task": train_task, "heldout_task": test_task,
                "before": {"outcome": before["outcome"], "answer": before["answer"], "execution": before.get("execution"),
                    "trace": before["trace"], "tool_outputs": before["tool_outputs"], "retrieved_tools": sorted(before_keys),
                    "edges": before_route["edges"], "resolved_entities": before_route["resolved_entities"],
                    "retrieval_ms": before_route["retrieval_ms"], "context_tokens_est": before_context,
                    "agent": before.get("agent")},
                "diagnosis": {"cause": diagnosis, "validated": True, "evidence": blocked,
                    "rules": ["dependency exists in declared Tool.dependencies", "real model selected dependent tool",
                        "execution runtime rejected the call because prerequisite was not retrieved"]},
                "proposal": {"tool": tool_key, "relationship": "DEPENDS_ON", "dependency": dependency,
                    "confidence": "deterministic_schema_and_runtime_validation", "expected_retrieval_effect": f"Retrieving {tool_key} will also retrieve {dependency}"},
                "mutation": {"id": update_id, "edge_identifier": dependency_edge_id(tool_key, dependency),
                    "edge": f"{tool_key} -[DEPENDS_ON]-> {dependency}", "relationship_type": "DEPENDS_ON",
                    "properties_before": baseline["properties"], "properties_experiment": experiment_state["properties"],
                    "properties_after": corrected_state["properties"], "state": "applied",
                    "semantics": "Existing edge stayed unchanged in HydraDB. The experiment passed an explicit retrieval filter that withheld this edge for one route; correction removed that filter. No edge write was performed.",
                    "evidence_source": "real Groq execution trace"},
                "graph_states": {"baseline": baseline, "experiment": experiment_state, "after_correction": corrected_state},
                "after": {"outcome": after_outcome, "answer": after["answer"], "execution": after.get("execution"),
                    "trace": after["trace"], "tool_outputs": after["tool_outputs"], "retrieved_tools": sorted(after_keys),
                    "edges": after_route["edges"], "resolved_entities": after_route["resolved_entities"],
                    "retrieval_ms": after_route["retrieval_ms"], "context_tokens_est": after_context,
                    "agent": after.get("agent")},
                "rollback_endpoint": f"/api/learning/rollback/{update_id}",
                "graph_restored_to_demo_start_on_rollback": baseline["weight"]}
        finally:
            if not correction_applied:
                restored = await get_dependency_state(tool_key, dependency)
                if not finished:
                    await finish_learning_experiment(update_id, state="interrupted_restored", evidence=[],
                        diagnosis="real_agent_experiment_did_not_complete", post_state=experiment_state,
                        rollback_state=restored)


@app.post("/api/learning/rollback/{update_id}")
async def rollback_learning(update_id: int):
    try:
        async with _learning_lock:
            _, rows, _ = await db.query(f"MATCH (u:LearningUpdate {{id: {update_id}}}) RETURN u.tool AS tool,u.dependency AS dependency,u.previous_weight AS previous_weight,u.applied_weight AS applied_weight,u.pre_state AS pre_state,u.post_state AS post_state,u.state AS state,u.edge_identifier AS edge_identifier")
            if not rows: raise HTTPException(404, detail="Learning update was not found.")
            tool, dependency, previous, applied, pre_json, post_json, state, edge_id = rows[0]
            if state != "applied": raise HTTPException(409, detail=f"Learning update is already {state}.")
            before = json.loads(pre_json) if pre_json else {"edge_exists": True, "weight": float(previous), "properties": {"weight": float(previous)}}
            expected_after = json.loads(post_json) if post_json else {"edge_exists": True, "weight": float(applied), "properties": {"weight": float(applied)}}
            current = await get_dependency_state(tool, dependency)
            if current != expected_after:
                raise HTTPException(409, detail=f"Graph state changed after this learning update; expected {expected_after}, observed {current}. No rollback was applied.")
            restored = await get_dependency_state(tool, dependency)
            if restored != before:
                raise HTTPException(409, detail=f"The dependency edge changed outside this retrieval experiment; expected {before}, observed {restored}. No rollback was applied.")
            await db.query(f"MATCH (u:LearningUpdate {{id: {update_id}}}) SET u.state = $state, u.rollback_state = $rollback_state, u.rolled_back_at = $rolled_back_at",
                {"state": "rolled_back", "rollback_state": json.dumps(restored, separators=(",", ":")), "rolled_back_at": time.time()})
            return {"update_id": update_id, "state": "rolled_back", "edge": f"{tool} -[DEPENDS_ON]-> {dependency}",
                "edge_identifier": edge_id or dependency_edge_id(tool, dependency), "relationship_type": "DEPENDS_ON",
                "properties_before": expected_after.get("properties"), "properties_after": restored.get("properties"),
                "restored_weight": restored["weight"], "restored_state": restored, "edge_written": False,
                "rollback_semantics": "Verified unchanged against the captured pre-experiment graph state; no dependency edge write was made."}
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
