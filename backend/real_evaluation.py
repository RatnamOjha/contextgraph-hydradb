"""Sequential, checkpointed real-Groq comparisons on frozen Olist prompts."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from .fixtures import DATA_TOOLS
from .groq_agent import run_groq_agent
from .olist_store import list_orders
from .objectives import evaluate_answer, requested_objectives
from .router import candidate_skills, flat_candidates, route_graph_candidates, tool_json

REAL_CASES = [
    ("line_items", "List line items, categories, and seller IDs for reference {id}.", "orders"),
    ("order_summary", "Summarize the order status and payment total for historical record {id}.", "orders"),
    ("payment_language", "I need the money trail: payment method, amount, and installments for record {id}.", "orders"),
]
STRATEGIES = ("full_context", "flat_lexical", "flat_dependency_expanded", "hydradb_graph", "hydradb_graph_after_learning")
log = logging.getLogger(__name__)


def _checkpoint_file(run_id: str) -> Path:
    root = Path(os.getenv("CONTEXTGRAPH_EVALUATION_CHECKPOINT_DIR", "data/evaluation-checkpoints"))
    return root / f"{run_id}.json"


def _local_applied_learning_updates() -> list[tuple[int, str, str]]:
    root = Path(os.getenv("CONTEXTGRAPH_LEARNING_STATE_DIR", "data/learning-updates"))
    updates = []
    if not root.exists(): return updates
    for path in root.glob("*.json"):
        try: record = json.loads(path.read_text())
        except (OSError, ValueError, TypeError): continue
        if record.get("state") == "applied" and record.get("learning_mode") == "real_groq":
            if record.get("tool") and record.get("dependency"):
                updates.append((int(record["id"]), record["tool"], record["dependency"]))
    return updates


async def _load_checkpoint(db, run_id: str) -> dict[str, Any] | None:
    candidates = []
    path = _checkpoint_file(run_id)
    if path.is_file():
        try:
            local = json.loads(path.read_text())
            candidates.append((int(local.get("_checkpoint_revision", 0)), local))
        except (OSError, ValueError, TypeError):
            log.warning("Ignoring unreadable local evaluation checkpoint %s", path)
    try:
        _, rows, _ = await db.query("MATCH (c:EvaluationCheckpoint {run_id: $run_id}) RETURN c.payload AS payload",
            {"run_id": run_id})
    except Exception as exc:
        log.warning("HydraDB checkpoint read failed for %s; using local checkpoint if present: %s", run_id, type(exc).__name__)
        rows = []
    checkpoints = []
    for (payload_json,) in rows:
        if not payload_json: continue
        try:
            payload = json.loads(payload_json)
            checkpoints.append((int(payload.get("_checkpoint_revision", 0)), payload))
        except (ValueError, TypeError):
            continue
    candidates.extend(checkpoints)
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


async def _save_checkpoint(db, run_id: str, payload: dict[str, Any]) -> None:
    revision = int(payload.get("_checkpoint_revision", 0)) + 1
    payload["_checkpoint_revision"] = revision
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    version_key = f"{run_id}:{revision}"
    try:
        await db.query("UNWIND $rows AS row MERGE (c {id: row.id}) SET c:EvaluationCheckpoint, c.run_id=row.run_id, c.revision=row.revision, c.state=row.state, c.updated_at=row.updated_at, c.payload=row.payload",
            {"rows": [{"id": db.id_for("EvaluationCheckpointVersion", version_key), "run_id": run_id,
                "revision": revision, "state": payload["state"], "updated_at": time.time(), "payload": serialized}]})
    except Exception as exc:
        # The local HydraDB HTTP runtime currently rejects this MERGE/SET batch.
        # Keep each completed pair resumable in an atomic local snapshot instead.
        log.warning("HydraDB checkpoint write failed for %s; persisting local snapshot: %s", run_id, type(exc).__name__)
    path = _checkpoint_file(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(serialized)
    temporary.replace(path)


def _normalize_checkpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """Add explicit accounting to prior checkpoints without discarding their evidence."""
    if payload.get("accounting_version", 0) >= 2:
        payload.setdefault("provider_attempts_unclassified", 0)
        payload.setdefault("provider_attempts_without_response", 0)
        payload.setdefault("provider_rate_limit_responses", 0)
        payload.setdefault("attempts_by_case_strategy", {})
        payload.setdefault("successful_responses_by_case_strategy", {})
        return payload
    attempted = int(payload.get("provider_requests_attempted", payload.get("provider_requests_used", 0)))
    known_successes = 0
    pair_attempts: dict[str, int] = {}
    for row in payload.get("results", []):
        execution = row.get("execution") or {}
        completed = int(execution.get("iterations", 0))
        attempts = int(row.get("provider_requests", completed))
        row["provider_requests_attempted"] = attempts
        row["provider_requests_succeeded"] = completed
        row["provider_responses_received"] = completed
        known_successes += completed
        pair_attempts[f"{row.get('case')}|{row.get('strategy')}"] = attempts
        task, answer, outputs = row.get("task", ""), row.get("answer", ""), row.get("tool_outputs", [])
        if task and answer and outputs:
            # Re-score stored evidence with the current deterministic evaluator. This
            # corrects the prior plural "dependencies" classifier bug without replaying Groq.
            row["outcome"] = evaluate_answer(task, answer, outputs,
                entity_resolved=bool(row.get("entity_resolution_correct", True)))
    unknown = max(0, attempted - sum(pair_attempts.values()))
    failed_case, failed_strategy = payload.get("failed_case"), payload.get("failed_strategy")
    if unknown and failed_case and failed_strategy:
        pair_attempts[f"{failed_case}|{failed_strategy}"] = unknown
    payload["accounting_version"] = 2
    payload["provider_requests_attempted"] = attempted
    payload["provider_responses_received"] = known_successes
    payload["provider_requests_succeeded"] = known_successes
    payload["provider_attempts_unclassified"] = unknown
    payload["provider_attempts_without_response"] = None if unknown else 0
    payload.setdefault("provider_rate_limit_responses", None if unknown else 0)
    payload["provider_requests_used"] = attempted  # backwards-compatible counter
    payload["attempts_by_case_strategy"] = pair_attempts
    payload["successful_responses_by_case_strategy"] = {
        f"{row.get('case')}|{row.get('strategy')}": int((row.get("execution") or {}).get("iterations", 0))
        for row in payload.get("results", [])}
    if unknown and failed_case and failed_strategy:
        payload["successful_responses_by_case_strategy"][f"{failed_case}|{failed_strategy}"] = None
    if unknown:
        payload["accounting_note"] = ("This checkpoint predates separate response accounting. Successful responses are counted from completed model iterations; "
            "remaining attempts and responses are unclassified and are not assumed to be successful or failed provider responses.")
        if "rate limit" in str(payload.get("provider_error", "")).lower():
            payload["provider_error_info"] = {"kind": "legacy_rate_limit_unclassified", "http_status": 429,
                "message": str(payload["provider_error"]), "retry": False}
    return payload


def _decorate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    completed = {(row.get("case"), row.get("strategy")): row for row in payload.get("results", [])
                 if row.get("state") == "complete"}
    statuses = []
    for case in payload.get("case_tasks", []):
        for strategy in payload.get("strategies", STRATEGIES):
            key = f"{case['name']}|{strategy}"
            row = completed.get((case["name"], strategy))
            if row:
                state = "COMPLETE"
                outcome = row.get("outcome", {}).get("code", "UNVERIFIED")
                error = None
                attempted = payload.get("attempts_by_case_strategy", {}).get(key,
                    row.get("provider_requests_attempted", row.get("provider_requests")))
                succeeded = payload.get("successful_responses_by_case_strategy", {}).get(key,
                    row.get("provider_requests_succeeded", (row.get("execution") or {}).get("iterations")))
            elif payload.get("failed_case") == case["name"] and payload.get("failed_strategy") == strategy:
                provider_error = payload.get("provider_error_info") or {}
                if provider_error.get("kind") == "request_budget_exhausted" or payload.get("state") == "paused_budget":
                    state = "NOT_RUN"
                elif provider_error.get("http_status") == 429 or "rate_limit" in str(provider_error.get("kind", "")) or provider_error.get("kind") in {"requests_per_minute", "tokens_per_minute", "daily_quota_exhausted", "rate_limit_unspecified", "legacy_rate_limit_unclassified"}:
                    state = "RATE_LIMITED"
                else:
                    state = "PROVIDER_ERROR"
                outcome, error = "NOT_RUN", payload.get("provider_error")
                attempted = payload.get("attempts_by_case_strategy", {}).get(key)
                succeeded = payload.get("successful_responses_by_case_strategy", {}).get(key)
            else:
                state, outcome, error, attempted, succeeded = "NOT_RUN", "NOT_RUN", None, 0, 0
            statuses.append({"case": case["name"], "strategy": strategy, "completion_status": state,
                "outcome": outcome, "provider_requests_attempted": attempted,
                "provider_requests_succeeded": succeeded, "provider_accounting_exact": succeeded is not None,
                "provider_error": error,
                "actual_tool_calls": row.get("actual_tool_calls") if row else None,
                "input_tokens": row.get("input_tokens") if row else None,
                "output_tokens": row.get("output_tokens") if row else None,
                "retrieval_ms": row.get("retrieval_ms") if row else None,
                "end_to_end_ms": row.get("end_to_end_ms") if row else None})
    payload["strategy_status"] = statuses
    summaries = {}
    for strategy in payload.get("strategies", STRATEGIES):
        rows = [row for row in payload.get("results", []) if row.get("strategy") == strategy and row.get("state") == "complete"]
        summaries[strategy] = {"cases_completed": len(rows),
            "verified_success_rate": round(sum(r["outcome"]["code"] == "SUCCESS" for r in rows)/len(rows), 3) if rows else None,
            "average_context_tokens": round(sum(r["context_tokens_est"] for r in rows)/len(rows), 1) if rows else None,
            "average_actual_tool_calls": round(sum(r["actual_tool_calls"] for r in rows)/len(rows), 2) if rows else None,
            "average_actual_tokens": round(sum(r["actual_tokens"] for r in rows)/len(rows), 1) if rows else None,
            "average_invalid_calls": round(sum(r["execution"].get("invalid_calls", 0) for r in rows)/len(rows), 2) if rows else None,
            "average_retrieval_ms": round(sum(r["retrieval_ms"] for r in rows)/len(rows), 2) if rows else None,
            "average_end_to_end_ms": round(sum(r["end_to_end_ms"] for r in rows)/len(rows), 2) if rows else None}
    payload["summary"] = summaries
    payload["provider_requests_remaining"] = max(0, int(payload.get("budget", {}).get("provider_requests", 0)) - int(payload.get("provider_requests_attempted", 0)))
    payload["provider_accounting_exact"] = int(payload.get("provider_attempts_unclassified", 0)) == 0
    payload["limitation"] = "The real-Groq comparative pilot is incomplete due to provider free-tier limits. No comparative superiority claim is made." if payload.get("state") != "complete" else "Small paired pilot; descriptive only. A completed run does not establish statistical significance."
    return payload


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
    try:
        applied_rows = await db.query("MATCH (u:LearningUpdate {state: 'applied', learning_mode: 'real_groq'}) RETURN u.id AS update_id,u.tool AS tool, u.dependency AS dependency")
        graph_updates = [(int(i), a, b) for i,a,b in applied_rows[1] if a and b]
    except Exception as exc:
        log.warning("HydraDB learning-state read failed; using local records: %s", type(exc).__name__)
        graph_updates = []
    learning_updates = sorted(set(graph_updates + _local_applied_learning_updates()))
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
            "provider_requests_attempted": 0, "provider_responses_received": 0, "provider_requests_succeeded": 0,
            "provider_attempts_unclassified": 0, "provider_attempts_without_response": 0, "provider_rate_limit_responses": 0,
            "attempts_by_case_strategy": {},
            "successful_responses_by_case_strategy": {}, "accounting_version": 2,
            "actual_tokens_used": 0, "tool_calls_used": 0, "budget": {"provider_requests": max_provider_requests,
                "actual_tokens": max_total_tokens, "tool_calls": max_tool_calls}}
    else:
        payload = _normalize_checkpoint(payload)
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
                _decorate_payload(payload)
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
                error_info = getattr(exc, "provider_error", None)
                payload["state"] = "paused_budget" if isinstance(error_info, dict) and error_info.get("kind") == "request_budget_exhausted" else "paused_provider_error"
                payload["provider_error"] = str(getattr(exc, "detail", type(exc).__name__))
                payload["provider_error_info"] = error_info if isinstance(error_info, dict) else {"kind": "provider_error_unknown"}
                payload["failed_case"] = case_name
                payload["failed_strategy"] = strategy
                attempted = int(getattr(exc, "provider_requests_attempted", getattr(exc, "provider_calls", 0)))
                succeeded = int(getattr(exc, "provider_requests_succeeded", 0))
                key = f"{case_name}|{strategy}"
                payload["provider_requests_attempted"] += attempted
                payload["provider_responses_received"] += int(getattr(exc, "provider_responses_received", succeeded))
                payload["provider_requests_succeeded"] += succeeded
                payload["provider_requests_used"] = payload["provider_requests_attempted"]
                payload["provider_attempts_without_response"] += max(0, attempted - int(getattr(exc, "provider_responses_received", succeeded)))
                if payload["provider_rate_limit_responses"] is not None:
                    payload["provider_rate_limit_responses"] += int(getattr(exc, "rate_limit_responses", 0))
                payload["attempts_by_case_strategy"][key] = payload["attempts_by_case_strategy"].get(key, 0) + attempted
                prior_succeeded = payload["successful_responses_by_case_strategy"].get(key, 0)
                payload["successful_responses_by_case_strategy"][key] = None if prior_succeeded is None else prior_succeeded + succeeded
                payload["actual_tokens_used"] += int(getattr(exc,"input_tokens",0))+int(getattr(exc,"output_tokens",0))
                _decorate_payload(payload)
                await _save_checkpoint(db, run_id, payload)
                return payload
            outcome = evaluate_answer(task, result["answer"], result["tool_outputs"], entity_resolved=True)
            usage = result.get("agent") or {}
            attempted = int(usage.get("provider_requests_attempted", usage.get("provider_calls", 0)))
            succeeded = int(usage.get("provider_requests_succeeded", usage.get("provider_responses_received", attempted)))
            payload["provider_requests_attempted"] += attempted
            payload["provider_responses_received"] += int(usage.get("provider_responses_received", succeeded))
            payload["provider_requests_succeeded"] += succeeded
            payload["provider_requests_used"] = payload["provider_requests_attempted"]
            if payload["provider_rate_limit_responses"] is not None:
                payload["provider_rate_limit_responses"] += int(usage.get("rate_limit_responses", 0))
            payload["actual_tokens_used"] += int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
            payload["tool_calls_used"] += int(result.get("tool_calls", 0))
            pair_key = f"{case_name}|{strategy}"
            payload["attempts_by_case_strategy"][pair_key] = payload["attempts_by_case_strategy"].get(pair_key, 0) + attempted
            prior_succeeded = payload["successful_responses_by_case_strategy"].get(pair_key, 0)
            payload["successful_responses_by_case_strategy"][pair_key] = None if prior_succeeded is None else prior_succeeded + succeeded
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
            row["provider_requests_attempted"] = attempted
            row["provider_requests_succeeded"] = succeeded
            row["provider_responses_received"] = int(usage.get("provider_responses_received", succeeded))
            payload["results"].append(row)
            if payload.get("failed_case") == case_name and payload.get("failed_strategy") == strategy:
                payload.pop("failed_case", None)
                payload.pop("failed_strategy", None)
                payload.pop("provider_error", None)
                payload.pop("provider_error_info", None)
            payload["state"] = "complete" if len(payload["results"]) == len(tasks)*len(STRATEGIES) else "running"
            _decorate_payload(payload)
            await _save_checkpoint(db, run_id, payload)
    payload["state"] = "complete"
    _decorate_payload(payload)
    await _save_checkpoint(db, run_id, payload)
    return payload
