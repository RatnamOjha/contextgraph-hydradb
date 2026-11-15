"""Sequential, checkpointed real-Groq comparisons on frozen Olist prompts."""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any

from .fixtures import DATA_TOOLS
from .groq_agent import run_groq_agent
from .olist_store import list_orders
from .objectives import evaluate_answer, requested_objectives
from .router import candidate_skills, expanded_flat_retrieve, flat_candidates, route_graph_candidates, tool_json

REAL_CASES = [
    ("line_items", "List line items, categories, and seller IDs for reference {id}.", "orders"),
    ("order_summary", "Summarize the order status and payment total for historical record {id}.", "orders"),
    ("payment_language", "I need the money trail: payment method, amount, and installments for record {id}.", "orders"),
]
STRATEGIES = ("full_context", "flat_lexical", "flat_dependency_expanded", "hydradb_graph", "hydradb_graph_after_learning")


async def _load_checkpoint(db, run_id: str) -> dict[str, Any] | None:
    _, rows, _ = await db.query(f"MATCH (c:EvaluationCheckpoint {{id: {db.id_for('EvaluationCheckpoint', run_id)}}}) RETURN c.payload AS payload")
    if not rows or not rows[0][0]: return None
    try: return json.loads(rows[0][0])
    except (ValueError, TypeError): return None


async def _save_checkpoint(db, run_id: str, payload: dict[str, Any]) -> None:
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    await db.query("UNWIND $rows AS row MERGE (c {id: row.id}) SET c:EvaluationCheckpoint, c.run_id=row.run_id, c.state=row.state, c.updated_at=row.updated_at, c.payload=row.payload",
        {"rows": [{"id": db.id_for("EvaluationCheckpoint", run_id), "run_id": run_id,
            "state": payload["state"], "updated_at": time.time(), "payload": serialized}]})


async def compare_real_agent(db, *, case_count: int = 1, max_provider_requests: int = 15,
                             max_total_tokens: int = 40000, max_tool_calls: int = 20,
                             resume: bool = True) -> dict[str, Any]:
    if not os.getenv("GROQ_API_KEY"):
        raise RuntimeError("Set GROQ_API_KEY on the API server for real-agent evaluation.")
    case_count = max(1, min(3, case_count))
    max_provider_requests = max(1, min(30, max_provider_requests))
    max_total_tokens = max(512, min(60000, max_total_tokens))
    max_tool_calls = max(1, min(60, max_tool_calls))
    orders = list_orders(200)
    heldout = orders[2:2+case_count]
    if len(heldout) < case_count: raise RuntimeError("Not enough frozen held-out Olist orders.")
    applied_rows = await db.query("MATCH (u:LearningUpdate {state: 'applied', learning_mode: 'real_groq'}) RETURN u.id AS update_id,u.tool AS tool, u.dependency AS dependency")
    learning_updates = sorted((int(i), a, b) for i,a,b in applied_rows[1] if a and b)
    learned_edges = {(a,b) for _,a,b in learning_updates}
    if not learned_edges:
        raise RuntimeError("Run the Real Agent Learning Demonstration first; graph-after-learning is not yet available.")
    tasks = [(REAL_CASES[i][0], REAL_CASES[i][1].format(id=heldout[i]["order_id"]), heldout[i]["order_id"]) for i in range(case_count)]
    model = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
    fingerprint = {"suite": "groq_paired_context_pilot_v1", "model": model, "cases": tasks,
        "strategies": STRATEGIES, "learning_updates": learning_updates}
    run_id = hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()[:24]
    payload = await _load_checkpoint(db, run_id) if resume else None
    if payload is None:
        payload = {"run_id": run_id, "suite": fingerprint["suite"], "state": "running", "model": model,
            "case_tasks": [{"name": n, "task": t, "order_id": oid} for n,t,oid in tasks],
            "strategies": list(STRATEGIES), "results": [], "provider_requests_used": 0,
            "actual_tokens_used": 0, "tool_calls_used": 0, "budget": {"provider_requests": max_provider_requests,
                "actual_tokens": max_total_tokens, "tool_calls": max_tool_calls}}
    else:
        payload["budget"]["provider_requests"] = max(payload["budget"].get("provider_requests",0),max_provider_requests)
        payload["budget"]["actual_tokens"] = max(payload["budget"].get("actual_tokens",0),max_total_tokens)
        payload["budget"]["tool_calls"] = max(payload["budget"].get("tool_calls",0),max_tool_calls)
        max_provider_requests=payload["budget"]["provider_requests"]
        max_total_tokens=payload["budget"]["actual_tokens"]
        max_tool_calls=payload["budget"]["tool_calls"]
    done = {(r["case"], r["strategy"]) for r in payload["results"] if r.get("state") == "complete"}
    tool_by_key = {tool.key: tool for tool in DATA_TOOLS}
    strategy_count = len(tasks)*len(STRATEGIES)
    provider_budget_per_run = max_provider_requests//strategy_count
    token_budget_per_run = max_total_tokens//strategy_count
    call_budget_per_run = max_tool_calls//strategy_count
    for case_name, task, order_id in tasks:
        candidates = candidate_skills(task)
        base_route = await route_graph_candidates(db, candidates, order_id)
        pre_route = await route_graph_candidates(db, candidates, order_id, exclude_dependencies=learned_edges)
        after_route = await route_graph_candidates(db, candidates, order_id)
        for strategy in STRATEGIES:
            if (case_name, strategy) in done: continue
            if provider_budget_per_run < 1 or token_budget_per_run < 512 or call_budget_per_run < 1 or payload["provider_requests_used"] + provider_budget_per_run > max_provider_requests or payload["actual_tokens_used"] + token_budget_per_run > max_total_tokens or payload["tool_calls_used"] + call_budget_per_run > max_tool_calls:
                payload["state"] = "paused_budget"
                await _save_checkpoint(db, run_id, payload)
                return payload
            t0 = time.perf_counter()
            route_ms = 0.0
            route = None
            if strategy == "full_context":
                selected = list(DATA_TOOLS)
            elif strategy == "flat_lexical":
                selected = flat_candidates(task, candidates, expand=False)
            elif strategy == "flat_dependency_expanded":
                selected = flat_candidates(task, candidates, expand=True)
            elif strategy == "hydradb_graph":
                route = pre_route; selected = [tool_by_key[row["key"]] for row in route["tools"] if row["key"] in tool_by_key]
            else:
                route = after_route; selected = [tool_by_key[row["key"]] for row in route["tools"] if row["key"] in tool_by_key]
            if route: route_ms = route["retrieval_ms"]
            elif strategy in {"flat_lexical", "flat_dependency_expanded"}: route_ms = (time.perf_counter()-t0)*1000
            context_tokens = max(1, sum(len(json.dumps(tool_json(tool), separators=(",", ":"))) for tool in selected)//4)
            started = time.perf_counter()
            try:
                result = await run_groq_agent(task, selected, required_order_id=order_id,
                    provider_request_budget=min(6, provider_budget_per_run), tool_call_budget=min(8, call_budget_per_run),
                    token_budget=min(60000, token_budget_per_run))
            except Exception as exc:
                payload["state"] = "paused_provider_error"
                payload["provider_error"] = str(getattr(exc, "detail", type(exc).__name__))
                payload["failed_case"] = case_name
                payload["failed_strategy"] = strategy
                payload["provider_requests_used"] += int(getattr(exc,"provider_calls",0))
                payload["actual_tokens_used"] += int(getattr(exc,"input_tokens",0))+int(getattr(exc,"output_tokens",0))
                await _save_checkpoint(db, run_id, payload)
                return payload
            outcome = evaluate_answer(task, result["answer"], result["tool_outputs"], entity_resolved=True)
            usage = result.get("agent") or {}
            payload["provider_requests_used"] += int(usage.get("provider_calls", 0))
            payload["actual_tokens_used"] += int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
            payload["tool_calls_used"] += int(result.get("tool_calls", 0))
            row = {"case": case_name, "task": task, "order_id": order_id, "strategy": strategy, "state": "complete",
                "outcome": outcome, "execution": result.get("execution", {}), "tools_retrieved": [tool.key for tool in selected],
                "tools_called": [r["tool"] for r in result["tool_outputs"]],
                "tools_executed": [r["tool"] for r in result["tool_outputs"] if not r.get("error")],
                "tool_outputs": result["tool_outputs"], "answer": result["answer"], "trace": result["trace"],
                "context_tokens_est": context_tokens, "actual_tokens": int(usage.get("input_tokens",0))+int(usage.get("output_tokens",0)),
                "actual_tool_calls": result.get("tool_calls",0),
                "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
                "retrieval_ms": round(route_ms,2), "end_to_end_ms": round((time.perf_counter()-started+route_ms/1000)*1000,2),
                "entity_resolution_correct": any(x["type"]=="order" and x["id"]==order_id for x in base_route["resolved_entities"]),
                "dependency_complete": all(set(t.dependencies).issubset({x.key for x in selected}) for t in selected),
                "provider_requests": usage.get("provider_calls",0)}
            payload["results"].append(row)
            payload["state"] = "complete" if len(payload["results"]) == len(tasks)*len(STRATEGIES) else "running"
            await _save_checkpoint(db, run_id, payload)
    payload["state"] = "complete"
    summaries = {}
    for strategy in STRATEGIES:
        rows = [r for r in payload["results"] if r["strategy"] == strategy]
        summaries[strategy] = {"cases_completed": len(rows),
            "verified_success_rate": round(sum(r["outcome"]["code"]=="SUCCESS" for r in rows)/len(rows),3) if rows else None,
            "average_context_tokens": round(sum(r["context_tokens_est"] for r in rows)/len(rows),1) if rows else None,
            "average_actual_tool_calls": round(sum(r["actual_tool_calls"] for r in rows)/len(rows),2) if rows else None,
            "average_actual_tokens": round(sum(r["actual_tokens"] for r in rows)/len(rows),1) if rows else None,
            "average_invalid_calls": round(sum(r["execution"].get("invalid_calls",0) for r in rows)/len(rows),2) if rows else None,
            "average_retrieval_ms": round(sum(r["retrieval_ms"] for r in rows)/len(rows),2) if rows else None,
            "average_end_to_end_ms": round(sum(r["end_to_end_ms"] for r in rows)/len(rows),2) if rows else None}
    payload["summary"] = summaries
    payload["limitation"] = "Small paired pilot; descriptive only. A completed run does not establish statistical significance."
    await _save_checkpoint(db, run_id, payload)
    return payload
