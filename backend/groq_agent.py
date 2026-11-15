"""Bounded Groq local-function-calling agent over retrieved Olist tools."""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from fastapi import HTTPException
import httpx

from .objectives import evaluate_answer, evidence_from_outputs, requested_objectives
from .olist_store import execute_data_tool
from .router import tool_json


def _schema(tool: Any) -> dict[str, Any]:
    return json.loads(tool.input_schema)


def _validate(tool: Any, args: Any) -> str | None:
    if not isinstance(args, dict): return "Tool arguments must be a JSON object."
    schema = _schema(tool)
    props = schema.get("properties", {})
    if set(args) - set(props): return "Unexpected arguments: " + ", ".join(sorted(set(args)-set(props)))
    missing = set(schema.get("required", [])) - set(args)
    if missing: return "Missing required arguments: " + ", ".join(sorted(missing))
    for name, value in args.items():
        typ = props[name].get("type")
        if typ == "string" and (not isinstance(value, str) or not value.strip()): return f"{name} must be a non-empty string."
        if typ == "integer" and (not isinstance(value, int) or isinstance(value, bool)): return f"{name} must be an integer."
        if typ == "number" and (not isinstance(value, (int, float)) or isinstance(value, bool)): return f"{name} must be a number."
        if typ == "boolean" and not isinstance(value, bool): return f"{name} must be a boolean."
    return None


async def run_groq_agent(task: str, selected_tools: list[Any], data_path: str | Path | None = None,
                         required_order_id: str | None = None, provider_request_budget: int | None = None,
                         tool_call_budget: int | None = None, token_budget: int | None = None) -> dict[str, Any]:
    if not os.getenv("GROQ_API_KEY"):
        raise HTTPException(503, detail="Groq agent unavailable: GROQ_API_KEY is not configured on the server.")
    model = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
    timeout = max(8, min(60, int(os.getenv("CONTEXTGRAPH_AGENT_TIMEOUT_SECONDS", "35"))))
    max_calls = max(1, min(8, tool_call_budget or int(os.getenv("CONTEXTGRAPH_MAX_AGENT_CALLS", "6"))))
    configured_turns = provider_request_budget if provider_request_budget is not None else int(os.getenv("CONTEXTGRAPH_MAX_AGENT_TURNS", "5"))
    max_turns = max(1, min(6, configured_turns))
    max_tokens = max(512, min(60000, token_budget if token_budget is not None else int(os.getenv("CONTEXTGRAPH_MAX_AGENT_TOKENS", "16000"))))
    by_name = {tool.key.replace(".", "__"): tool for tool in selected_tools}
    by_key = {tool.key: tool for tool in selected_tools}
    if not by_key: raise HTTPException(422, detail="No executable dataset tools were retrieved for this request.")
    definitions = [{"type": "function", "function": {"name": key,
        "description": tool.description + (" Prerequisite tools to execute first: " + ", ".join(dep for dep in tool.dependencies if dep in by_key) + "." if any(dep in by_key for dep in tool.dependencies) else ""),
        "parameters": _schema(tool)}} for key, tool in by_name.items()]
    objectives = requested_objectives(task)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "You are ContextGraph, an order-support agent. Use only the provided retrieved tools. Choose tools and arguments yourself; do not call tools that are unnecessary. Execute prerequisite tools before dependent tools. Use returned tool results for factual claims. Do not invent facts, infer non-delivery from a missing delivery timestamp, or confuse item totals with payment totals. Clearly label the simulated ContextGraph refund rule and never describe it as an Olist policy. The source timestamps do not specify a timezone; preserve them as source-reported timestamps."},
        {"role": "user", "content": task},
    ]
    outputs: list[dict[str, Any]] = []
    trace: list[dict[str, Any]] = [{"step": "groq-agent", "message": f"{model} received {len(definitions)} HydraDB-retrieved tool definitions"}]
    completed: set[str] = set()
    total_input = total_output = 0
    errors: list[dict[str, str]] = []
    calls_used = 0
    provider_attempts = 0
    seen_calls: set[tuple[str, str]] = set()
    repeated_calls = 0
    invalid_calls = 0
    execution_failures = 0
    last_answer = ""
    started = time.perf_counter()
    deadline = time.monotonic() + timeout
    client = httpx.AsyncClient(timeout=httpx.Timeout(timeout), headers={
        "Authorization": f"Bearer {os.environ['GROQ_API_KEY']}", "Content-Type": "application/json"})

    async def invoke() -> Any:
        nonlocal provider_attempts
        remaining = max(0.1, deadline - time.monotonic())
        attempts = 0
        while True:
          async with asyncio.timeout(remaining):
            provider_attempts += 1
            response = await client.post("https://api.groq.com/openai/v1/chat/completions", json={
                "model": model, "messages": messages, "tools": definitions,
                "tool_choice": "auto",
                "parallel_tool_calls": False, "temperature": 0, "max_completion_tokens": max(32, min(512, max_tokens-total_input-total_output)),
            })
            if response.status_code == 429:
                attempts += 1
                retry_after = float(response.headers.get("retry-after", "0") or 0)
                max_retries = max(0, min(1, int(os.getenv("CONTEXTGRAPH_PROVIDER_MAX_RETRIES", "1"))))
                if attempts <= max_retries and 0 < retry_after <= 2 and time.monotonic()+retry_after < deadline:
                    await asyncio.sleep(retry_after)
                    remaining = max(0.1, deadline-time.monotonic())
                    continue
                raise HTTPException(429, detail="Groq rate limit reached; no retry window remained within the bounded request budget.")
            if response.is_error:
                raise HTTPException(502, detail=f"Groq request failed ({response.status_code}); check model access and provider status.")
            return response.json()

    try:
        for turn in range(max_turns):
            if total_input + total_output + 32 > max_tokens: break
            try:
                response = await invoke()
            except (TimeoutError, httpx.TimeoutException) as exc:
                failure = HTTPException(504, detail="Groq request timed out; no fixture fallback was run.")
                failure.provider_calls = provider_attempts
                failure.input_tokens = total_input
                failure.output_tokens = total_output
                raise failure from exc
            except Exception as exc:
                if isinstance(exc, HTTPException):
                    exc.provider_calls = provider_attempts
                    exc.input_tokens = total_input
                    exc.output_tokens = total_output
                    raise
                raise HTTPException(502, detail=f"Groq request failed ({type(exc).__name__}); check model access and provider status.") from exc
            usage = response.get("usage") or {}
            total_input += usage.get("prompt_tokens", 0)
            total_output += usage.get("completion_tokens", 0)
            message = response["choices"][0]["message"]
            calls = message.get("tool_calls") or []
            if not calls:
                answer = (message.get("content") or "").strip()
                if not answer: raise HTTPException(502, detail="Groq returned no final answer.")
                last_answer = answer
                scored = evaluate_answer(task, answer, outputs, entity_resolved=bool(required_order_id))
                incomplete = [row for row in scored["objectives"] if row["factually_correct"] is not True]
                # If sufficient evidence was collected but the narrative omitted or misstated it,
                # allow one evidence-targeted revision within the same bounded turn budget.
                if incomplete and turn + 1 < max_turns and calls_used < max_calls:
                    feedback = [{"objective": row["label"], "reason": row["reason"], "evidence_sufficient": row["evidence_sufficient"]} for row in incomplete]
                    messages.append({"role": "assistant", "content": answer})
                    messages.append({"role": "user", "content": "Revise the answer using the tool evidence already returned. These objectives are not verified: " + json.dumps(feedback) + ". If retrieved evidence cannot establish one, say it is unverified. You may call another retrieved tool only if its result can supply missing evidence."})
                    continue
                trace.append({"step": "agent-answer", "message": f"Groq completed in {provider_attempts} provider request(s); {scored['verified_objectives']}/{scored['total_objectives']} objectives verified"})
                return {"success": scored["code"] == "SUCCESS", "answer": answer, "tool_calls": calls_used,
                    "tool_outputs": outputs, "trace": trace, "missing_tools": [],
                    "dependency_failures": [], "reused_entities": [],
                    "outcome": scored, "execution": {"tools_exposed": sorted(by_key), "tools_called": sorted({r["tool"] for r in outputs}),
                        "tools_executed": sorted({r["tool"] for r in outputs if not r["error"]}),
                        "invalid_calls": invalid_calls, "repeated_calls": repeated_calls, "execution_failures": execution_failures, "iterations": turn+1},
                    "agent": {"provider": "Groq", "model": model, "provider_calls": provider_attempts,
                        "input_tokens": total_input, "output_tokens": total_output,
                        "elapsed_ms": round((time.perf_counter()-started)*1000, 2)}}
            messages.append({"role": "assistant", "content": message.get("content"), "tool_calls": calls})
            for call in calls:
                name = call["function"]["name"]
                tool = by_name.get(name)
                args = None
                try: args = json.loads(call["function"].get("arguments") or "{}")
                except json.JSONDecodeError: args = None
                fingerprint = (name, json.dumps(args, sort_keys=True, separators=(",", ":")))
                if fingerprint in seen_calls:
                    repeated_calls += 1
                    result = {"error": "Repeated identical tool call; choose a different retrieved tool or summarize the available evidence."}
                    errors.append({"type": "repeated_call", "tool": name})
                elif calls_used >= max_calls:
                    result = {"error": f"Tool-call budget ({max_calls}) reached."}
                    errors.append({"type": "call_budget", "tool": name})
                else:
                    calls_used += 1
                    if tool is None:
                        result = {"error": "Tool is not in the retrieved HydraDB context."}
                        errors.append({"type": "tool_not_retrieved", "tool": name}); invalid_calls += 1
                    elif (validation := _validate(tool, args)):
                        result = {"error": validation}
                        errors.append({"type": "invalid_arguments", "tool": name}); invalid_calls += 1
                    else:
                        blocked = [dep for dep in tool.dependencies if dep not in completed]
                        if blocked:
                            result = {"error": "Required tool dependencies were not executed first: " + ", ".join(blocked)}
                            errors.append({"type": "missing_dependency", "tool": name}); execution_failures += 1
                        elif required_order_id and args.get("order_id") and args["order_id"] != required_order_id:
                            result = {"error": "Requested order ID differs from the task's resolved order."}
                            errors.append({"type": "entity_mismatch", "tool": name}); invalid_calls += 1
                        else:
                            result = execute_data_tool(tool.key, args, data_path)
                            if "error" in result:
                                errors.append({"type": "data_miss", "tool": name}); execution_failures += 1
                            else:
                                completed.add(tool.key)
                                seen_calls.add(fingerprint)
                key = name.replace("__", ".")
                is_error = "error" in result
                outputs.append({"tool": key, "arguments": args, "result": result, "error": is_error})
                trace.append({"step": key, "message": "Groq-selected tool call" if not is_error else "Tool call rejected", "arguments": args, "result": result})
                messages.append({"role": "tool", "tool_call_id": call["id"], "name": name, "content": json.dumps(result, ensure_ascii=False)})
            evidence = evidence_from_outputs(outputs)
            missing = [o for o in objectives if not evidence.get(o["id"], {}).get("sufficient")]
            if missing and turn + 1 < max_turns and calls_used < max_calls:
                pending_dependencies = []
                for row in outputs:
                    message = str(row.get("result", {}).get("error", ""))
                    match = re.search(r"Required tool dependencies were not executed first: (.+)", message)
                    if match:
                        pending_dependencies.extend(dep.strip() for dep in match.group(1).split(","))
                actionable = sorted({dep for dep in pending_dependencies if dep in by_key and dep not in completed})
                if actionable:
                    followup = "The runtime rejected a dependent call. The prerequisite tool(s) " + ", ".join(actionable) + " are in your retrieved context but have not been executed. Execute the necessary prerequisite first, then retry the original call once."
                elif pending_dependencies:
                    followup = "The runtime reported prerequisite(s) " + ", ".join(sorted(set(pending_dependencies))) + " that were not included in the retrieved tools. Do not retry that dependent call. Explain the objective as unverified with this evidence gap."
                else:
                    followup = "Evidence still missing for requested objective(s): " + "; ".join(o["label"] for o in missing) + ". Continue by selecting a retrieved tool only if it can supply this source evidence. Do not call every tool. If no retrieved tool can verify it, provide a final answer that labels it unverified."
                messages.append({"role": "user", "content": followup})
            elif objectives and turn + 1 < max_turns:
                messages.append({"role": "user", "content": "The requested objectives now have sufficient source evidence in the tool results above. Do not make additional tool calls. Give a concise final answer that states only facts supported by those results, and do not say the retrieved data is missing."})
        missing_labels = [row["label"] for row in objectives if not evidence_from_outputs(outputs).get(row["id"], {}).get("sufficient")]
        answer = (last_answer + "\n\n" if last_answer else "") + "Unverified objective(s) within the bounded agent budget: " + (", ".join(missing_labels) if missing_labels else "final answer verification") + "."
        scored = evaluate_answer(task, answer, outputs, entity_resolved=bool(required_order_id))
        trace.append({"step": "agent-budget", "message": f"Stopped after {max_turns} provider requests and {calls_used} tool calls; unsupported objectives remain explicit."})
        return {"success": False, "answer": answer, "tool_calls": calls_used, "tool_outputs": outputs,
            "trace": trace, "missing_tools": [], "dependency_failures": [], "reused_entities": [], "outcome": scored,
            "execution": {"tools_exposed": sorted(by_key), "tools_called": sorted({r["tool"] for r in outputs}),
                "tools_executed": sorted({r["tool"] for r in outputs if not r["error"]}),
                "invalid_calls": invalid_calls, "repeated_calls": repeated_calls, "execution_failures": execution_failures, "iterations": max_turns},
            "agent": {"provider": "Groq", "model": model, "provider_calls": provider_attempts,
                "input_tokens": total_input, "output_tokens": total_output,
                "elapsed_ms": round((time.perf_counter()-started)*1000, 2)}}
    finally:
        await client.aclose()
