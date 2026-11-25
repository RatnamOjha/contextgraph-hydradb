from __future__ import annotations

import json
import logging
import os
import re
import time
import asyncio
import hashlib
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from .config import load_env_file

load_env_file()

from .fixtures import DATA_TOOLS, EVALUATION_CASES, SKILLS, TASKS, TRAINING_TASKS, make_catalog
from .hydra import HydraClient, HydraError
from .registry import inspect_tools, seed_catalog, seed_olist_entities
from .developer_catalog import register_catalog_tools
from .catalog_retrieval import retrieve_catalog_context
from .router import candidate_skills, classify, execute_fixture, expanded_flat_retrieve, flat_candidates, flat_retrieve, route_graph, route_graph_candidates, tool_json
from .olist_store import DEFAULT_DB, available as olist_available, dataset_status, list_orders, lookup_order
from .dataset_workflow import execute_deterministic, extract_order_id, required_tools
from .groq_agent import run_groq_agent
from .objectives import evaluate_answer, requested_objectives
from .real_evaluation import compare_real_agent

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("contextgraph")
db = HydraClient()
_learning_lock = asyncio.Lock()
_agent_eval_lock = asyncio.Lock()
_catalog_registration_lock = asyncio.Lock()
_learning_state_dir = Path(os.getenv("CONTEXTGRAPH_LEARNING_STATE_DIR", "data/learning-updates"))


def _learning_state_path(update_id: int) -> Path:
    return _learning_state_dir / f"{int(update_id)}.json"


def _write_learning_state(update_id: int, record: dict) -> None:
    global _learning_state_dir
    payload = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    target = _learning_state_path(update_id)
    try:
        _learning_state_dir.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(payload)
        temporary.replace(target)
    except OSError:
        fallback = Path(tempfile.gettempdir()) / "contextgraph-learning-updates"
        if fallback == _learning_state_dir: raise
        fallback.mkdir(parents=True, exist_ok=True)
        temporary = fallback / f"{int(update_id)}.tmp"
        target = fallback / f"{int(update_id)}.json"
        temporary.write_text(payload)
        temporary.replace(target)
        _learning_state_dir = fallback


def _read_learning_state(update_id: int) -> dict | None:
    try:
        return json.loads(_learning_state_path(update_id).read_text())
    except (OSError, ValueError, TypeError):
        return None


def _local_learning_experiments() -> list[dict]:
    try:
        if not _learning_state_dir.exists(): return []
        paths = _learning_state_dir.glob("*.json")
    except OSError:
        return []
    rows = []
    for path in paths:
        try: rows.append(json.loads(path.read_text()))
        except (OSError, ValueError, TypeError): continue
    return rows


class SeedRequest(BaseModel):
    catalog_size: Literal[20, 100, 500] = 20
    reset: bool = True
    catalog_scope: Literal["combined", "company"] = "combined"


class RegisterToolDefinition(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=4000)
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    server: str | None = Field(default=None, max_length=80)
    # None means "leave the current dependency edges unchanged". An explicit
    # empty list is the caller's instruction to clear those edges.
    prerequisites: list[str] | None = Field(default=None, max_length=100)


class RegisterToolsRequest(BaseModel):
    tools: list[RegisterToolDefinition] = Field(min_length=1, max_length=200)


class CatalogRetrievalRequest(BaseModel):
    task: str = Field(min_length=8, max_length=1200)
    max_tools: int = Field(default=8, ge=1, le=50)
    known_tool_ids: list[str] = Field(default_factory=list, max_length=50)
    exclude_tool_ids: list[str] = Field(default_factory=list, max_length=50)


class RunRequest(BaseModel):
    task: str = Field(min_length=8, max_length=2000)
    method: Literal["full", "flat", "graph", "ablation", "expanded_flat"] = "graph"
    catalog_size: Literal[20, 100, 500] = 20
    execution_mode: Literal["fixture", "agent"] = "fixture"
    catalog_scope: Literal["combined", "company"] = "combined"


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
app.mount("/assets", StaticFiles(directory=WEB), name="web-assets")


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
        result = await seed_catalog(db, request.catalog_size, reset=request.reset, catalog_scope=request.catalog_scope)
        log.info("catalog_seeded %s", result)
        return {"status": "seeded", "catalog_size": request.catalog_size, **result}
    except Exception as exc:
        raise as_http_error(exc) from exc


@app.get("/api/tools")
async def tools():
    try:
        rows = await inspect_tools(db)
        executable = {tool.key: tool for tool in DATA_TOOLS}
        _, edge_rows, _ = await db.query("MATCH (a:Tool)-[:DEPENDS_ON]->(b:Tool) RETURN a.key AS source,b.key AS target LIMIT 10000")
        dependency_map: dict[str, list[str]] = {}
        for source, target in edge_rows:
            dependency_map.setdefault(str(source), []).append(str(target))
        for row in rows:
            tool = executable.get(row["key"])
            if tool:
                row["execution_status"] = "simulated_policy" if tool.server == "contextgraph-simulated" else "dataset_backed"
                row["source"] = "ContextGraph demo policy" if tool.server == "contextgraph-simulated" else "Olist historical dataset"
            elif row.get("provenance") == "developer_registered":
                row["execution_status"] = "metadata_only"
                row["source"] = "Developer registered catalog metadata"
            else:
                row["execution_status"] = "legacy_fixture_only"
                row["source"] = "Synthetic legacy fixture; excluded from Groq agent tools"
            row["dependencies"] = sorted(set(dependency_map.get(row["key"], [])))
        return {"tools": rows}
    except Exception as exc:
        raise as_http_error(exc) from exc


def _local_catalog_registration_allowed(request: Request) -> bool:
    """Writes require explicit dev opt-in, a loopback caller, and no Render runtime."""
    configured = (os.getenv("CONTEXTGRAPH_ENV", "").lower() == "development"
        and os.getenv("CONTEXTGRAPH_ENABLE_LOCAL_REGISTRATION") == "1"
        and "RENDER" not in os.environ and "RENDER_SERVICE_ID" not in os.environ)
    host = request.client.host if request.client else ""
    return configured and host in {"127.0.0.1", "::1", "localhost", "testclient"}


@app.post("/api/v1/catalogs/{catalog}/tools")
async def register_tools(catalog: str, request: Request, body: RegisterToolsRequest):
    if not _local_catalog_registration_allowed(request):
        raise HTTPException(status_code=403,
            detail="Developer catalog writes are disabled; enable trusted loopback development mode.")
    try:
        async with _catalog_registration_lock:
            return await register_catalog_tools(db, catalog,
                [tool.model_dump(exclude_unset=True) for tool in body.tools])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise as_http_error(exc) from exc


@app.post("/api/v1/catalogs/{catalog}/retrieve")
async def retrieve_developer_catalog(catalog: str, body: CatalogRetrievalRequest):
    """Return bounded graph context for one explicitly selected developer catalog."""
    try:
        _, tool_rows, _ = await db.query(
            "MATCH (t:Tool {catalog: $catalog}) RETURN t.key AS key,t.catalog AS catalog,t.name AS name,"
            "t.description AS description,t.schema AS schema,t.input_schema AS input_schema,"
            "t.output_schema AS output_schema,t.server AS server,t.provenance AS provenance ORDER BY t.key LIMIT 201",
            {"catalog": catalog})
        if len(tool_rows) > 200:
            raise HTTPException(status_code=422, detail="Catalog exceeds the 200-tool retrieval bound")
        tools = [dict(zip(["key", "catalog", "name", "description", "schema", "input_schema",
            "output_schema", "server", "provenance"], row)) for row in tool_rows]
        if not tools:
            raise HTTPException(status_code=404, detail=f"No registered tools found for catalog {catalog!r}")
        _, catalog_rows, _ = await db.query("MATCH (c:Catalog {key: $catalog}) RETURN c.key AS key LIMIT 1", {"catalog": catalog})
        if not catalog_rows:
            raise HTTPException(status_code=404, detail=f"No registered catalog found for {catalog!r}")
        _, edge_rows, _ = await db.query(
            "MATCH (a:Tool {catalog: $catalog})-[:DEPENDS_ON]->(b:Tool) RETURN a.key AS source,b.key AS target LIMIT 1000",
            {"catalog": catalog})
        dependencies = [(str(source), str(target)) for source, target in edge_rows]
        _, server_rows, _ = await db.query(
            "MATCH (t:Tool {catalog: $catalog})-[:PROVIDED_BY]->(s:MCPServer) RETURN t.key AS tool,s.key AS server LIMIT 200",
            {"catalog": catalog})
        servers = [(str(tool), str(server)) for tool, server in server_rows]
        _, catalog_edge_rows, _ = await db.query(
            "MATCH (t:Tool {catalog: $catalog})-[:IN_CATALOG]->(c:Catalog) RETURN t.key AS tool,c.key AS catalog LIMIT 200",
            {"catalog": catalog})
        catalog_edges = [(str(tool), str(key)) for tool, key in catalog_edge_rows]
        return retrieve_catalog_context(catalog=catalog, task=body.task, tools=tools,
            dependencies=dependencies, servers=servers, catalog_edges=catalog_edges, max_tools=body.max_tools,
            known_tool_ids=body.known_tool_ids, exclude_tool_ids=body.exclude_tool_ids)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
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


async def record_learning_execution(*args, **kwargs) -> None:
    try:
        await record_dataset_execution(*args, **kwargs)
    except Exception as exc:
        # Learning is a read/retrieval experiment; optional execution-trace writes
        # must not leave the user without the evidence already collected.
        log.warning("Learning execution trace could not be written to HydraDB: %s", type(exc).__name__)


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
        route = await route_graph_candidates(db, candidates, order_id,
            root_tool_keys=set(required_tools(request.task)))
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
    outcome = evaluate_answer(request.task, agent_result["answer"], agent_result["tool_outputs"],
        entity_resolved=entity_match, fixture_mode=request.execution_mode == "fixture")
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
    catalog = make_catalog(request.catalog_size, scope=request.catalog_scope)
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


def _validate_retrieval_edge(route: dict, tool_key: str, dependency_key: str, *, expected: bool, stage: str) -> None:
    relation_rows = [edge for edge in route.get("edges", [])
        if edge.get("type") == "DEPENDS_ON" and edge.get("from") == tool_key and edge.get("to") == dependency_key]
    if len(relation_rows) > 1:
        raise HTTPException(409, detail=f"{stage} returned the dependency edge more than once; refusing to treat a duplicate route as validated.")
    selected = {row.get("key") for row in route.get("tools", [])}
    observed = len(relation_rows) == 1 and dependency_key in selected
    if observed != expected:
        state = "present" if observed else "absent"
        wanted = "present" if expected else "absent"
        raise HTTPException(409, detail=f"The dependency edge was {state} in {stage} retrieval; expected it to be {wanted}.")


def _decode_edge_snapshot(value: str | dict | None) -> dict | None:
    if isinstance(value, dict): return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, dict) else None
        except (TypeError, ValueError):
            return None
    return None


def _legacy_weight_snapshot(value: Any) -> dict | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        weight = float(value)
        return {"edge_exists": True, "weight": weight, "properties": {"weight": weight}}
    return None


async def get_dependency_state(tool_key: str, dependency_key: str) -> dict:
    tool_id = db.id_for("Tool", tool_key)
    dependency_id = db.id_for("Tool", dependency_key)
    _, rows, _ = await db.query(f"MATCH (a:Tool {{id: {tool_id}}})-[r:DEPENDS_ON]->(b:Tool {{id: {dependency_id}}}) RETURN r.weight AS weight")
    if not rows:
        raise HTTPException(409, detail=f"Expected dependency edge {tool_key} → {dependency_key} is absent; the demo will not create or delete it.")
    if len(rows) != 1:
        raise HTTPException(409, detail=f"Expected one dependency edge for {tool_key} → {dependency_key}, found {len(rows)}; refusing an ambiguous relationship state.")
    raw_weight = rows[0][0] if rows[0] else None
    if raw_weight is None:
        # A returned row proves the relationship exists. HydraDB projects both
        # an absent property and a null property as None; neither is a weight.
        return {"edge_exists": True, "weight": None, "weight_state": "absent_or_null", "properties": {}}
    if isinstance(raw_weight, bool) or not isinstance(raw_weight, (int, float)):
        raise HTTPException(409, detail=f"Dependency edge weight has an unsupported value ({type(raw_weight).__name__}); refusing to change or infer its state.")
    weight = float(raw_weight)
    return {"edge_exists": True, "weight": weight, "properties": {"weight": weight}}


async def verify_dependency_state_unchanged(tool_key: str, dependency_key: str, snapshot: dict) -> dict:
    if not snapshot.get("edge_exists"):
        raise HTTPException(409, detail="The original dependency edge was absent; restoration would require creating a relationship, which this demo does not do.")
    current = await get_dependency_state(tool_key, dependency_key)
    if current != snapshot:
        raise HTTPException(409, detail=f"Dependency edge changed during a retrieval-only experiment: expected {snapshot}, observed {current}. No relationship write was attempted.")
    return current


async def create_learning_experiment(update_id: int, tool: str, dependency: str, learning_mode: str,
                                     training: str, heldout: str, snapshot: dict, original_task: str | None = None) -> None:
    snapshot_json = json.dumps(snapshot, separators=(",", ":"))
    experiment_state = {**snapshot, "retrieval_exclusion": {"from": tool, "relationship_type": "DEPENDS_ON", "to": dependency}}
    record = {"id": update_id, "tool": tool, "dependency": dependency, "learning_mode": learning_mode,
        "training_order": training, "heldout_order": heldout, "edge_identifier": dependency_edge_id(tool, dependency),
        "relationship_type": "DEPENDS_ON", "previous_weight": snapshot.get("weight"), "applied_weight": snapshot.get("weight"),
        "pre_state": snapshot, "experiment_state": experiment_state, "state": "experimenting", "evidence": {},
        "original_task": original_task or training,
        "graph_version": hashlib.sha256(snapshot_json.encode()).hexdigest(),
        "proposed_graph_correction": {"tool": tool, "relationship_type": "DEPENDS_ON", "dependency": dependency,
            "declared_in_tool_metadata": True}}
    _write_learning_state(update_id, record)
    try:
        fields = ["u:LearningUpdate", "u.tool=row.tool", "u.dependency=row.dependency", "u.learning_mode=row.learning_mode",
            "u.training_order=row.training_order", "u.heldout_order=row.heldout_order", "u.edge_identifier=row.edge_identifier",
            "u.relationship_type=row.relationship_type", "u.pre_state=row.pre_state", "u.experiment_state=row.experiment_state",
            "u.state=row.state", "u.evidence=row.evidence", "u.original_task=row.original_task", "u.graph_version=row.graph_version",
            "u.proposed_graph_correction=row.proposed_graph_correction"]
        row_data = {"id": update_id, "tool": tool, "dependency": dependency, "learning_mode": learning_mode,
                "training_order": training, "heldout_order": heldout, "edge_identifier": record["edge_identifier"],
                "relationship_type": "DEPENDS_ON",
                "pre_state": snapshot_json, "experiment_state": json.dumps(experiment_state, separators=(",", ":")),
                "state": "experimenting", "evidence": "{}", "original_task": record["original_task"],
                "graph_version": record["graph_version"], "proposed_graph_correction": json.dumps(record["proposed_graph_correction"], separators=(",", ":"))}
        if isinstance(snapshot.get("weight"), (int, float)) and not isinstance(snapshot.get("weight"), bool):
            fields.extend(["u.previous_weight=row.previous_weight", "u.applied_weight=row.applied_weight"])
            row_data.update({"previous_weight": float(snapshot["weight"]), "applied_weight": float(snapshot["weight"])})
        await db.query("UNWIND $rows AS row MERGE (u {id: row.id}) SET " + ", ".join(fields), {"rows": [row_data]})
        _, rows, _ = await db.query("MATCH (u:LearningUpdate {id: $id}) RETURN u.tool AS tool,u.dependency AS dependency,u.pre_state AS pre_state,u.experiment_state AS experiment_state,u.state AS state,u.original_task AS original_task,u.graph_version AS graph_version,u.proposed_graph_correction AS proposed_graph_correction",
            {"id": update_id})
        if not rows or rows[0] != [tool, dependency, snapshot_json,
                json.dumps(experiment_state, separators=(",", ":")), "experimenting", record["original_task"],
                record["graph_version"], json.dumps(record["proposed_graph_correction"], separators=(",", ":"))]:
            raise RuntimeError("HydraDB learning record readback did not match the experiment snapshot")
        record["evidence_store"] = "HydraDB_readback_confirmed"
    except Exception as exc:
        record["evidence_store"] = "local_sidecar"
        log.warning("HydraDB learning record create failed for %s; keeping local evidence record: %s", update_id, type(exc).__name__)
    _write_learning_state(update_id, record)


async def finish_learning_experiment(update_id: int, *, state: str, evidence: dict | list,
                                     diagnosis: str, post_state: dict | None = None,
                                     rollback_state: dict | None = None,
                                     retrieval_snapshot: dict | None = None,
                                     heldout_retest: dict | None = None,
                                     rollback_verification: dict | None = None) -> None:
    record = _read_learning_state(update_id) or {"id": update_id}
    if retrieval_snapshot is not None: record["retrieval_snapshot"] = retrieval_snapshot
    if heldout_retest is not None: record["heldout_retest"] = heldout_retest
    if rollback_verification is not None: record["rollback_verification"] = rollback_verification
    evidence_record = {"original_task": record.get("original_task"),
        "retrieval_snapshot": record.get("retrieval_snapshot") or record.get("experiment_state"),
        "actual_execution_failure": evidence, "proposed_graph_correction": record.get("proposed_graph_correction"),
        "validation_evidence": diagnosis, "correction_status": state,
        "graph_version": record.get("graph_version"), "heldout_retest": record.get("heldout_retest"),
        "rollback_information": rollback_state or record.get("rollback_state"),
        "rollback_verification": record.get("rollback_verification")}
    if record.get("retrieval_snapshot") and record.get("heldout_retest"):
        retrieval = record["retrieval_snapshot"]
        evidence_record["learning_sequence"] = [
            {"stage": "baseline", "edge_state": record.get("pre_state"),
                "retrieved_tools": retrieval.get("baseline_tools", [])},
            {"stage": "dependency_withheld_from_retrieval", "retrieved_tools": retrieval.get("training_tools", []),
                "excluded_edge": record.get("experiment_state", {}).get("retrieval_exclusion")},
            {"stage": "validated_correction", "validated": state == "applied", "edge_state": post_state},
            {"stage": "heldout_retest", "evidence": record["heldout_retest"]},
            {"stage": "rollback_verification", "evidence": record.get("rollback_verification"),
                "edge_state": rollback_state},
        ]
    record.update({"state": state, "evidence": evidence, "evidence_record": evidence_record, "diagnosis": diagnosis,
        "post_state": post_state, "rollback_state": rollback_state, "finished_at": time.time()})
    try:
        assignments = ["u.state = $state", "u.evidence = $evidence", "u.diagnosis = $diagnosis", "u.finished_at = $finished_at"]
        parameters = {"state": state, "evidence": json.dumps(evidence_record, ensure_ascii=False),
            "diagnosis": diagnosis, "finished_at": record["finished_at"]}
        if post_state is not None:
            assignments.append("u.post_state = $post_state")
            parameters["post_state"] = json.dumps(post_state, separators=(",", ":"))
        if rollback_state is not None:
            assignments.append("u.rollback_state = $rollback_state")
            parameters["rollback_state"] = json.dumps(rollback_state, separators=(",", ":"))
        await db.query(f"MATCH (u:LearningUpdate {{id: {update_id}}}) SET " + ", ".join(assignments), parameters)
        _, rows, _ = await db.query("MATCH (u:LearningUpdate {id: $id}) RETURN u.state AS state,u.evidence AS evidence,u.diagnosis AS diagnosis,u.post_state AS post_state,u.rollback_state AS rollback_state",
            {"id": update_id})
        expected = [state, json.dumps(evidence_record, ensure_ascii=False), diagnosis,
            json.dumps(post_state, separators=(",", ":")) if post_state is not None else None,
            json.dumps(rollback_state, separators=(",", ":")) if rollback_state is not None else None]
        if not rows or rows[0] != expected:
            raise RuntimeError("HydraDB learning evidence readback did not match the execution evidence")
        if record.get("evidence_store") != "local_sidecar": record["evidence_store"] = "HydraDB_readback_confirmed"
    except Exception as exc:
        record["evidence_store"] = "local_sidecar"
        log.warning("HydraDB learning record update failed for %s; keeping local evidence record: %s", update_id, type(exc).__name__)
    _write_learning_state(update_id, record)


async def recover_interrupted_learning_experiments() -> None:
    records = {int(row["id"]): row for row in _local_learning_experiments() if row.get("state") == "experimenting"}
    try:
        _, rows, _ = await db.query("MATCH (u:LearningUpdate {state: 'experimenting'}) RETURN u.id AS id,u.tool AS tool,u.dependency AS dependency,u.pre_state AS pre_state,u.experiment_state AS experiment_state")
    except Exception as exc:
        log.warning("HydraDB learning recovery query failed; checking local evidence records: %s", type(exc).__name__)
        rows = []
    for update_id, tool, dependency, raw_snapshot, raw_experiment in rows:
        records.setdefault(int(update_id), {"id": int(update_id), "tool": tool, "dependency": dependency,
            "pre_state": json.loads(raw_snapshot) if raw_snapshot else None,
            "experiment_state": json.loads(raw_experiment) if raw_experiment else {}, "state": "experimenting",
            "evidence_store": "HydraDB"})
    for update_id, record in records.items():
        tool, dependency = record.get("tool"), record.get("dependency")
        snapshot = record.get("pre_state")
        experiment = record.get("experiment_state") or {}
        if not tool or not dependency or not snapshot:
            raise RuntimeError(f"Learning experiment {update_id} lacks a recovery snapshot; refusing to serve with unknown graph state.")
        if isinstance(snapshot, str): snapshot = json.loads(snapshot)
        current = await get_dependency_state(tool, dependency)
        if "retrieval_exclusion" in experiment:
            if current != snapshot:
                raise RuntimeError(f"Retrieval-only learning experiment {update_id} found graph drift; refusing automatic mutation.")
            restored = current
        else:
            # Legacy and current hosted learning records are retrieval-only too.
            # Never repair them by writing the shared dependency edge.
            if current != snapshot:
                raise RuntimeError(f"Legacy learning experiment {update_id} found graph drift; refusing to mutate the shared dependency edge.")
            restored = current
        record["pre_state"] = snapshot
        record["post_state"] = restored
        record["rollback_state"] = restored
        record["recovered_at"] = time.time()
        record["state"] = "interrupted_restored"
        _write_learning_state(update_id, record)
        try:
            await db.query(f"MATCH (u:LearningUpdate {{id: {int(update_id)}}}) SET u.state = $state, u.recovered_at = $recovered_at, u.rollback_state = $rollback_state",
                {"state": "interrupted_restored", "recovered_at": record["recovered_at"], "rollback_state": json.dumps(restored, separators=(",", ":"))})
        except Exception as exc:
            log.warning("HydraDB recovery marker failed for %s; local record updated: %s", update_id, type(exc).__name__)
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
        baseline_route = await route_graph_candidates(db, candidates, training,
            root_tool_keys=set(required_tools(train_task)))
        _validate_retrieval_edge(baseline_route, tool_key, dependency, expected=True, stage="baseline")
        update_id = int(time.time_ns() & ((1 << 62) - 1)) or 3
        await create_learning_experiment(update_id, tool_key, dependency, "fixture", training, heldout, baseline, train_task)
        finished = False
        try:
            experiment_state = {**baseline, "retrieval_exclusion": {"from": tool_key, "relationship_type": "DEPENDS_ON", "to": dependency}}
            before_route = await route_graph_candidates(db, candidates, training,
                exclude_dependencies={(tool_key, dependency)}, root_tool_keys=set(required_tools(train_task)))
            _validate_retrieval_edge(before_route, tool_key, dependency, expected=False, stage="dependency-withheld")
            by_key = {tool.key: tool for tool in DATA_TOOLS}
            before_tools = [by_key[row["key"]] for row in before_route["tools"] if row["key"] in by_key]
            before = execute_deterministic(train_task, training, before_tools, DEFAULT_DB)
            await record_learning_execution(train_task, "graph:fixture:dependency-withheld", before["success"], candidates,
                [tool.key for tool in before_tools], before["tool_outputs"], before_route["resolved_entities"], 0.0, None)
            evidence = before["dependency_failures"]
            declared = next((tool for tool in DATA_TOOLS if tool.key == tool_key), None)
            if not any(row.get("tool") == tool_key and row.get("missing_dependency") == dependency for row in evidence) or not declared or dependency not in declared.dependencies:
                restored = await verify_dependency_state_unchanged(tool_key, dependency, baseline)
                await finish_learning_experiment(update_id, state="no_update", evidence=evidence,
                    diagnosis="No validated dependency evidence", post_state=experiment_state, rollback_state=restored,
                    rollback_verification={"verified": True, "unchanged_from_baseline": True, "edge_written": False})
                finished = True
                return {"state": "no_update", "mode": "fixture", "diagnosis": "No validated evidence; the dependency was restored to baseline.",
                    "evidence": evidence, "evidence_store": (_read_learning_state(update_id) or {}).get("evidence_store"),
                    "graph_states": {"baseline": baseline, "experiment": experiment_state, "rollback": restored},
                    "rollback_verification": {"verified": True, "unchanged_from_baseline": True, "edge_written": False}}

            corrected_state = await verify_dependency_state_unchanged(tool_key, dependency, baseline)
            if corrected_state != baseline:
                raise HTTPException(409, detail="HydraDB did not confirm restoration of the dependency's baseline state.")
            await finish_learning_experiment(update_id, state="applied", evidence=evidence,
                diagnosis="dependency_withheld_from_retrieval_then_filter_removed", post_state=corrected_state)
            after_candidates = candidate_skills(test_task)
            after_route = await route_graph_candidates(db, after_candidates, heldout,
                root_tool_keys=set(required_tools(test_task)))
            _validate_retrieval_edge(after_route, tool_key, dependency, expected=True, stage="held-out retest")
            after_tools = [by_key[row["key"]] for row in after_route["tools"] if row["key"] in by_key]
            after = execute_deterministic(test_task, heldout, after_tools, DEFAULT_DB)
            await record_learning_execution(test_task, "graph:fixture:after-learning", after["success"], after_candidates,
                [tool.key for tool in after_tools], after["tool_outputs"], after_route["resolved_entities"], 0.0, None)
            rollback_state = await verify_dependency_state_unchanged(tool_key, dependency, baseline)
            rollback_verification = {"verified": True, "edge_exists": rollback_state["edge_exists"],
                "unchanged_from_baseline": rollback_state == baseline, "edge_written": False,
                "semantics": "The retrieval-only experiment left the persisted relationship unchanged."}
            await finish_learning_experiment(update_id, state="applied", evidence=evidence,
                diagnosis="controlled_dependency_recovery_filter_removed", post_state=corrected_state,
                retrieval_snapshot={"baseline_tools": [row["key"] for row in baseline_route["tools"]],
                    "baseline_edges": baseline_route["edges"], "training_tools": [tool.key for tool in before_tools], "edges": before_route["edges"],
                    "resolved_entities": before_route["resolved_entities"]},
                heldout_retest={"task": test_task, "order_id": heldout, "success": after["success"],
                    "selected_tools": [tool.key for tool in after_tools], "tool_outputs": after["tool_outputs"],
                    "edges": after_route["edges"]}, rollback_state=rollback_state,
                rollback_verification=rollback_verification)
            finished = True
            return {"state": "applied", "update_id": update_id, "mode": "fixture", "training_order_id": training, "heldout_order_id": heldout,
                "evidence_store": (_read_learning_state(update_id) or {}).get("evidence_store"),
                "before": {"task": train_task, "success": before["success"], "selected_tools": [t.key for t in before_tools], "trace": before["trace"], "dependency_failures": evidence},
                "proposal": {"tool": tool_key, "relationship": "DEPENDS_ON", "dependency": dependency, "evidence": evidence,
                    "validation": "matched declared tool prerequisite and observed fixture execution dependency failure"},
                "relationship_experiment": {"id": update_id, "edge_identifier": dependency_edge_id(tool_key, dependency),
                    "edge": f"{tool_key} -[DEPENDS_ON]-> {dependency}", "relationship_type": "DEPENDS_ON",
                    "properties_before": baseline["properties"], "properties_experiment": experiment_state["properties"],
                    "properties_after": corrected_state["properties"], "state": "applied",
                    "semantics": "Retrieval-filter demonstration only: the persisted relationship was not mutated. One route excluded it; correction removed the request-scoped filter."},
                "graph_states": {"baseline": baseline, "experiment": experiment_state, "after_correction": corrected_state,
                    "rollback": rollback_state}, "rollback_verification": rollback_verification,
                "retrieval_states": {"baseline": baseline_route, "withheld": before_route, "heldout_retest": after_route},
                "after": {"task": test_task, "success": after["success"], "selected_tools": [t.key for t in after_tools], "trace": after["trace"], "dependency_failures": after["dependency_failures"]},
                "heldout_is_disjoint": training != heldout, "rollback_endpoint": f"/api/learning/rollback/{update_id}"}
        finally:
            if not finished:
                restored = await verify_dependency_state_unchanged(tool_key, dependency, baseline)
                await finish_learning_experiment(update_id, state="interrupted_restored", evidence=[],
                    diagnosis="fixture_experiment_did_not_complete", rollback_state=restored,
                    rollback_verification={"verified": True, "unchanged_from_baseline": True, "edge_written": False})


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
        baseline_route = await route_graph_candidates(db, candidates, training,
            root_tool_keys=set(required_tools(train_task)))
        _validate_retrieval_edge(baseline_route, tool_key, dependency, expected=True, stage="baseline")
        declared = next((tool for tool in DATA_TOOLS if tool.key == tool_key), None)
        if not declared or dependency not in declared.dependencies:
            raise HTTPException(409, detail="The catalog no longer declares the dependency required for this controlled demonstration.")
        update_id = int(time.time_ns() & ((1 << 62) - 1)) or 3
        await create_learning_experiment(update_id, tool_key, dependency, "real_groq", training, heldout, baseline, train_task)
        finished = False
        experiment_state = None
        try:
            experiment_state = {**baseline, "retrieval_exclusion": {"from": tool_key, "relationship_type": "DEPENDS_ON", "to": dependency}}
            before_route = await route_graph_candidates(db, candidates, training,
                exclude_dependencies={(tool_key, dependency)}, root_tool_keys=set(required_tools(train_task)))
            _validate_retrieval_edge(before_route, tool_key, dependency, expected=False, stage="dependency-withheld")
            by_key = {tool.key: tool for tool in DATA_TOOLS}
            before_keys = {row["key"] for row in before_route["tools"] if row["key"] in by_key}
            before_tools = [by_key[key] for key in sorted(before_keys)]
            try:
                before = await run_groq_agent(train_task, before_tools, DEFAULT_DB, training,
                    provider_request_budget=3, tool_call_budget=4)
            except HTTPException as exc:
                restored = await verify_dependency_state_unchanged(tool_key, dependency, baseline)
                await finish_learning_experiment(update_id, state="provider_failure", evidence=[],
                    diagnosis=str(getattr(exc, "provider_error", {}).get("kind", "provider_error")),
                    post_state=experiment_state, rollback_state=restored,
                    rollback_verification={"verified": True, "unchanged_from_baseline": True, "edge_written": False})
                finished = True
                return {"state": "provider_failure", "mode": "real_groq", "message": exc.detail,
                    "provider_error": getattr(exc, "provider_error", None), "graph_restored": True,
                    "mutation_applied": False, "training_order_id": training,
                    "evidence_store": (_read_learning_state(update_id) or {}).get("evidence_store"),
                    "retrieved_tools": sorted(before_keys), "retrieval_ms": before_route["retrieval_ms"],
                    "graph_states": {"baseline": baseline, "experiment": experiment_state, "rollback": restored},
                    "rollback_verification": {"verified": True, "unchanged_from_baseline": True, "edge_written": False},
                    "relationship_experiment": {"id": update_id, "edge_identifier": dependency_edge_id(tool_key, dependency),
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
            await record_learning_execution(train_task, "graph:real-agent-learning:before", before["success"], candidates,
                sorted(before_keys), before["tool_outputs"], before_route["resolved_entities"], before_route["retrieval_ms"], before.get("agent"))
            diagnosis = "missing_dependency" if blocked else ("dataset_limitation" if before["outcome"]["total_objectives"] and all(not o["evidence_sufficient"] for o in before["outcome"]["objectives"]) else ("model_reasoning_failure" if before["tool_calls"] == 0 else "execution_or_answer_failure"))
            if before["outcome"]["code"] == "SUCCESS":
                diagnosis = "valid_alternative_path"
            if not blocked:
                restored = await verify_dependency_state_unchanged(tool_key, dependency, baseline)
                await finish_learning_experiment(update_id, state="no_update", evidence=before["tool_outputs"],
                    diagnosis=diagnosis, post_state=experiment_state, rollback_state=restored,
                    rollback_verification={"verified": True, "unchanged_from_baseline": True, "edge_written": False})
                finished = True
                return {"state": "no_update", "mode": "real_groq", "diagnosis": diagnosis,
                    "explanation": "The real run did not produce validated evidence that this prerequisite is missing; no graph mutation was made.",
                    "evidence_store": (_read_learning_state(update_id) or {}).get("evidence_store"),
                    "training_order_id": training, "before": before, "retrieval": {"tools": sorted(before_keys),
                        "edges": before_route["edges"], "resolved_entities": before_route["resolved_entities"], "retrieval_ms": before_route["retrieval_ms"]},
                    "graph_restored": True, "graph_states": {"baseline": baseline, "experiment": experiment_state, "rollback": restored},
                    "rollback_verification": {"verified": True, "unchanged_from_baseline": True, "edge_written": False},
                    "relationship_experiment": {"id": update_id, "edge_identifier": dependency_edge_id(tool_key, dependency),
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
            after_candidates = candidate_skills(test_task)
            after_route = await route_graph_candidates(db, after_candidates, heldout,
                root_tool_keys=set(required_tools(test_task)))
            _validate_retrieval_edge(after_route, tool_key, dependency, expected=True, stage="held-out retest")
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
            await record_learning_execution(test_task, "graph:real-agent-learning:after", after["success"], after_candidates,
                sorted(after_keys), after["tool_outputs"], after_route["resolved_entities"], after_route["retrieval_ms"], after.get("agent"))
            rollback_state = await verify_dependency_state_unchanged(tool_key, dependency, baseline)
            rollback_verification = {"verified": True, "edge_exists": rollback_state["edge_exists"],
                "unchanged_from_baseline": rollback_state == baseline, "edge_written": False,
                "semantics": "The retrieval-only experiment left the persisted relationship unchanged."}
            await finish_learning_experiment(update_id, state="applied", evidence=evidence,
                diagnosis="controlled_dependency_recovery_filter_removed", post_state=corrected_state,
                retrieval_snapshot={"baseline_tools": [row["key"] for row in baseline_route["tools"]],
                    "baseline_edges": baseline_route["edges"], "training_tools": sorted(before_keys), "edges": before_route["edges"],
                    "resolved_entities": before_route["resolved_entities"]},
                heldout_retest={"task": test_task, "order_id": heldout, "outcome": after_outcome,
                    "retrieved_tools": sorted(after_keys), "tool_outputs": after["tool_outputs"],
                    "edges": after_route["edges"]}, rollback_state=rollback_state,
                rollback_verification=rollback_verification)
            before_context = max(1, sum(len(json.dumps(tool_json(t), separators=(",", ":"))) for t in before_tools)//4)
            after_context = max(1, sum(len(json.dumps(tool_json(t), separators=(",", ":"))) for t in after_tools)//4)
            finished = True
            return {"state": "applied", "mode": "real_groq", "update_id": update_id,
                "evidence_store": (_read_learning_state(update_id) or {}).get("evidence_store"),
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
                "relationship_experiment": {"id": update_id, "edge_identifier": dependency_edge_id(tool_key, dependency),
                    "edge": f"{tool_key} -[DEPENDS_ON]-> {dependency}", "relationship_type": "DEPENDS_ON",
                    "properties_before": baseline["properties"], "properties_experiment": experiment_state["properties"],
                    "properties_after": corrected_state["properties"], "state": "applied",
                    "semantics": "Retrieval-filter demonstration only: the persisted relationship was not mutated. One route excluded it; correction removed the request-scoped filter. No edge write was performed.",
                    "evidence_source": "real Groq execution trace"},
                "graph_states": {"baseline": baseline, "experiment": experiment_state, "after_correction": corrected_state,
                    "rollback": rollback_state}, "rollback_verification": rollback_verification,
                "retrieval_states": {"baseline": baseline_route, "withheld": before_route, "heldout_retest": after_route},
                "after": {"outcome": after_outcome, "answer": after["answer"], "execution": after.get("execution"),
                    "trace": after["trace"], "tool_outputs": after["tool_outputs"], "retrieved_tools": sorted(after_keys),
                    "edges": after_route["edges"], "resolved_entities": after_route["resolved_entities"],
                    "retrieval_ms": after_route["retrieval_ms"], "context_tokens_est": after_context,
                    "agent": after.get("agent")},
                "rollback_endpoint": f"/api/learning/rollback/{update_id}",
                "graph_restored_to_demo_start_on_rollback": baseline}
        finally:
            if not finished:
                restored = await verify_dependency_state_unchanged(tool_key, dependency, baseline)
                await finish_learning_experiment(update_id, state="interrupted_restored", evidence=[],
                    diagnosis="real_agent_experiment_did_not_complete", post_state=experiment_state,
                    rollback_state=restored,
                    rollback_verification={"verified": True, "unchanged_from_baseline": True, "edge_written": False})


@app.post("/api/learning/rollback/{update_id}")
async def rollback_learning(update_id: int):
    try:
        async with _learning_lock:
            try:
                _, rows, _ = await db.query(f"MATCH (u:LearningUpdate {{id: {update_id}}}) RETURN u.tool AS tool,u.dependency AS dependency,u.previous_weight AS previous_weight,u.applied_weight AS applied_weight,u.pre_state AS pre_state,u.post_state AS post_state,u.state AS state,u.edge_identifier AS edge_identifier,u.evidence AS evidence")
            except Exception as exc:
                log.warning("HydraDB rollback lookup failed for %s; checking local evidence: %s", update_id, type(exc).__name__)
                rows = []
            local = _read_learning_state(update_id)
            if rows:
                tool, dependency, previous, applied, pre_json, post_json, state, edge_id, evidence_json = rows[0]
                before = _decode_edge_snapshot(pre_json) or _decode_edge_snapshot((local or {}).get("pre_state")) or _legacy_weight_snapshot(previous)
                expected_after = _decode_edge_snapshot(post_json) or _decode_edge_snapshot((local or {}).get("post_state")) or _decode_edge_snapshot((local or {}).get("rollback_state")) or _legacy_weight_snapshot(applied) or before
                if before is None or expected_after is None:
                    raise HTTPException(409, detail="Learning update has no recoverable edge snapshot; refusing to infer a missing weight or apply a rollback.")
                record_store = "HydraDB"
                local = local or {"id": update_id, "tool": tool, "dependency": dependency, "previous_weight": previous,
                    "applied_weight": applied, "pre_state": before, "post_state": expected_after, "state": state,
                    "edge_identifier": edge_id, "evidence_store": "HydraDB",
                    "evidence_record": json.loads(evidence_json) if evidence_json else {}}
            elif local:
                tool, dependency, state, edge_id = local.get("tool"), local.get("dependency"), local.get("state"), local.get("edge_identifier")
                before = _decode_edge_snapshot(local.get("pre_state")) or _legacy_weight_snapshot(local.get("previous_weight"))
                expected_after = _decode_edge_snapshot(local.get("post_state")) or _decode_edge_snapshot(local.get("rollback_state")) or _legacy_weight_snapshot(local.get("applied_weight")) or before
                if before is None or expected_after is None:
                    raise HTTPException(409, detail="Learning update has no recoverable edge snapshot; refusing to infer a missing weight or apply a rollback.")
                record_store = local.get("evidence_store", "local_sidecar")
            else:
                raise HTTPException(404, detail="Learning update was not found.")
            if state != "applied": raise HTTPException(409, detail=f"Learning update is already {state}.")
            current = await get_dependency_state(tool, dependency)
            if current != expected_after:
                raise HTTPException(409, detail=f"Graph state changed after this learning update; expected {expected_after}, observed {current}. No rollback was applied.")
            restored = await get_dependency_state(tool, dependency)
            if restored != before:
                raise HTTPException(409, detail=f"The dependency edge changed outside this retrieval experiment; expected {before}, observed {restored}. No rollback was applied.")
            if local:
                local.update({"state": "rolled_back", "rollback_state": restored, "rolled_back_at": time.time()})
                if not isinstance(local.get("evidence_record"), dict):
                    local["evidence_record"] = {"legacy_evidence": local.get("evidence", [])}
                local["evidence_record"]["rollback_information"] = restored
                local["evidence_record"]["correction_status"] = "rolled_back"
                _write_learning_state(update_id, local)
            if rows:
                try:
                    rollback_json = json.dumps(restored, separators=(",", ":"))
                    evidence_json = json.dumps((local or {}).get("evidence_record", {}), ensure_ascii=False)
                    await db.query(f"MATCH (u:LearningUpdate {{id: {update_id}}}) SET u.state = $state, u.rollback_state = $rollback_state, u.rolled_back_at = $rolled_back_at, u.evidence = $evidence",
                        {"state": "rolled_back", "rollback_state": rollback_json, "rolled_back_at": time.time(), "evidence": evidence_json})
                    _, verify_rows, _ = await db.query("MATCH (u:LearningUpdate {id: $id}) RETURN u.state AS state,u.rollback_state AS rollback_state,u.evidence AS evidence",
                        {"id": update_id})
                    if not verify_rows or verify_rows[0] != ["rolled_back", rollback_json, evidence_json]:
                        raise RuntimeError("HydraDB rollback marker readback mismatch")
                    record_store = "HydraDB_readback_confirmed"
                except Exception as exc:
                    record_store = "local_sidecar"
                    log.warning("HydraDB rollback marker failed for %s; local state updated: %s", update_id, type(exc).__name__)
            return {"update_id": update_id, "state": "rolled_back", "edge": f"{tool} -[DEPENDS_ON]-> {dependency}",
                "edge_identifier": edge_id or dependency_edge_id(tool, dependency), "relationship_type": "DEPENDS_ON",
                "properties_before": expected_after.get("properties"), "properties_after": restored.get("properties"),
                "restored_weight": restored.get("weight"), "restored_state": restored, "edge_written": False,
                "rollback_semantics": "Verified unchanged against the captured pre-experiment graph state; no dependency edge write was made.",
                "evidence_store": record_store}
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


@app.get("/api/graph/explore")
async def graph_explore(task: str = "What is the status of this order?", order_id: str | None = None):
    """Return a bounded, read-only subgraph for an Olist task and optional order."""
    if len(task) < 8 or len(task) > 1200:
        raise HTTPException(422, detail="Task must contain 8–1200 characters.")
    if order_id and not re.fullmatch(r"[0-9a-f]{32}", order_id, re.I):
        raise HTTPException(422, detail="Order ID must be a 32-character Olist identifier.")
    try:
        candidates = candidate_skills(task)
        route = await route_graph_candidates(db, candidates, order_id, max_depth=4,
            root_tool_keys=set(required_tools(task)))
        nodes: dict[str, dict[str, Any]] = {}
        edges: dict[tuple[str, str, str], dict[str, str]] = {}

        def add_node(key: str, kind: str, **metadata: Any) -> None:
            if key and key not in nodes and len(nodes) < 150:
                nodes[key] = {"id": key, "label": key, "type": kind, "metadata": metadata}

        def add_edge(source: str, target: str, relation: str) -> None:
            if source in nodes and target in nodes and len(edges) < 300:
                edges[(source, relation, target)] = {"from": source, "to": target, "type": relation}

        for candidate in candidates:
            key = candidate["skill"]
            add_node(key, "Skill", score=candidate["score"], evidence=candidate["evidence"])
        for tool in route["tools"]:
            key = tool["key"]
            definition = next((item for item in DATA_TOOLS if item.key == key), None)
            add_node(key, "Tool", name=tool.get("name"), description=tool.get("description"),
                input_schema=tool.get("schema"), server=tool.get("server"), mcp_server=tool.get("mcp"),
                dependencies=list(definition.dependencies) if definition else [],
                why=tool.get("why", []),
                execution_provenance=("simulated_policy" if definition and definition.server == "contextgraph-simulated"
                    else "dataset_backed" if definition else "legacy_fixture_only"))
        for edge in route["edges"]:
            source, target = edge.get("from"), edge.get("to")
            if source in nodes and target not in nodes:
                if edge.get("type") == "PROVIDED_BY":
                    add_node(target, "MCPServer", name=target, transport="stdio", protocol="MCP")
                else:
                    add_node(target, "Tool")
            elif target in nodes and source not in nodes and edge.get("type") == "REQUIRES":
                add_node(source, "TaskType")
            add_edge(source, target, edge.get("type", "RELATED"))

        # Include only actual Agent -> Skill relationships for the candidate skills.
        if candidates:
            candidate_ids = [db.id_for("Skill", row["skill"]) for row in candidates]
            _, agent_rows, _ = await db.query(
                f"MATCH (a:Agent)-[:HAS_SKILL]->(s:Skill) WHERE s.id IN {candidate_ids} "
                "RETURN a.key AS agent,s.key AS skill,a.name AS name,a.description AS description LIMIT 12")
            for agent_key, skill_key, name, description in agent_rows:
                add_node(agent_key, "Agent", name=name, description=description)
                add_edge(agent_key, skill_key, "HAS_SKILL")

        resolved = route.get("resolved_entities", [])
        entity_keys = {}
        for entity in resolved:
            kind, external = entity["type"], entity["id"]
            key = f"{kind}:{external}"
            entity_keys[(kind, external)] = key
            add_node(key, "Entity", entity_type=kind, external_id=external, source="olist")
        order_key = entity_keys.get(("order", order_id)) if order_id else None
        customer = next((entity for entity in resolved if entity["type"] == "customer"), None)
        if order_key and customer:
            customer_key = entity_keys.get(("customer", customer["id"]))
            add_edge(order_key, customer_key, "BELONGS_TO")

        # The order neighborhood is capped and read directly from the seeded Olist entity graph.
        if order_id and order_key:
            order_node_id = db.id_for("Entity", f"order:{order_id}")
            _, item_rows, _ = await db.query(
                "MATCH (o:Entity {id: $id})-[:HAS_ITEM]->(i:Entity) "
                "RETURN o.key AS order_key,i.id AS item_id,i.key AS item_key,i.external_id AS external_id LIMIT 30",
                {"id": order_node_id})
            item_ids = []
            for _, item_id, item_key, external_id in item_rows:
                item_id = int(item_id)
                item_ids.append(item_id)
                add_node(item_key, "Entity", entity_type="order_item", external_id=external_id, source="olist")
                add_edge(order_key, item_key, "HAS_ITEM")
            if item_ids:
                _, product_rows, _ = await db.query(
                    f"MATCH (i:Entity)-[:ITEM_PRODUCT]->(p:Entity) WHERE i.id IN {sorted(set(item_ids))} "
                    "RETURN i.key AS item_key,p.key AS product_key,p.external_id AS product_id LIMIT 60")
                for item_key, product_key, product_id in product_rows:
                    add_node(product_key, "Entity", entity_type="product", external_id=product_id, source="olist")
                    add_edge(item_key, product_key, "ITEM_PRODUCT")
                _, seller_rows, _ = await db.query(
                    f"MATCH (i:Entity)-[:SOLD_BY]->(s:Entity) WHERE i.id IN {sorted(set(item_ids))} "
                    "RETURN i.key AS item_key,s.key AS seller_key,s.external_id AS seller_id LIMIT 60")
                for item_key, seller_key, seller_id in seller_rows:
                    add_node(seller_key, "Entity", entity_type="seller", external_id=seller_id, source="olist")
                    add_edge(item_key, seller_key, "SOLD_BY")

        return {"task": task, "order_id": order_id, "skills": route["skills"],
            "nodes": list(nodes.values()), "edges": list(edges.values()),
            "resolved_entities": resolved, "retrieval_ms": round(route.get("retrieval_ms", 0), 3),
            "bounded": {"max_nodes": 150, "max_edges": 300, "order_items": 30}}
    except HTTPException:
        raise
    except Exception as exc:
        raise as_http_error(exc) from exc
