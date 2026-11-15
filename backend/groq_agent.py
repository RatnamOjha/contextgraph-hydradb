"""Bounded Groq local-function-calling agent over retrieved Olist tools."""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from fastapi import HTTPException
import httpx

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
                         required_order_id: str | None = None) -> dict[str, Any]:
    if not os.getenv("GROQ_API_KEY"):
        raise HTTPException(503, detail="Groq agent unavailable: GROQ_API_KEY is not configured on the server.")
    model = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
    timeout = max(8, min(60, int(os.getenv("CONTEXTGRAPH_AGENT_TIMEOUT_SECONDS", "35"))))
    max_calls = max(1, min(8, int(os.getenv("CONTEXTGRAPH_MAX_AGENT_CALLS", "6"))))
    max_turns = 4
    by_name = {tool.key.replace(".", "__"): tool for tool in selected_tools}
    by_key = {tool.key: tool for tool in selected_tools}
    if not by_key: raise HTTPException(422, detail="No executable dataset tools were retrieved for this request.")
    definitions = [{"type": "function", "function": {"name": key, "description": tool.description,
        "parameters": _schema(tool)}} for key, tool in by_name.items()]
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "You are ContextGraph, an order-support agent. Use the provided tools for every factual claim. The historical source is Olist 2016–2018. Do not invent missing data. Clearly label the simulated ContextGraph refund rule and never describe it as an Olist policy. Ask for a supported order/customer ID if evidence is unavailable."},
        {"role": "user", "content": task},
    ]
    outputs: list[dict[str, Any]] = []
    trace: list[dict[str, Any]] = [{"step": "groq-agent", "message": f"{model} received {len(definitions)} HydraDB-retrieved tool definitions"}]
    completed: set[str] = set()
    total_input = total_output = 0
    errors: list[str] = []
    calls_used = 0
    started = time.perf_counter()
    deadline = time.monotonic() + timeout
    client = httpx.AsyncClient(timeout=httpx.Timeout(timeout), headers={
        "Authorization": f"Bearer {os.environ['GROQ_API_KEY']}", "Content-Type": "application/json"})

    async def invoke() -> Any:
        remaining = max(0.1, deadline - time.monotonic())
        async with asyncio.timeout(remaining):
            response = await client.post("https://api.groq.com/openai/v1/chat/completions", json={
                "model": model, "messages": messages, "tools": definitions,
                "tool_choice": "required" if not outputs else "auto",
                "parallel_tool_calls": False, "temperature": 0, "max_completion_tokens": 512,
            })
            if response.status_code == 429:
                raise HTTPException(429, detail="Groq free-tier rate limit reached; try again later.")
            if response.is_error:
                raise HTTPException(502, detail=f"Groq request failed ({response.status_code}); check model access and provider status.")
            return response.json()

    try:
        for turn in range(max_turns):
            try:
                response = await invoke()
            except (TimeoutError, httpx.TimeoutException) as exc:
                raise HTTPException(504, detail="Groq request timed out; no fixture fallback was run.") from exc
            except Exception as exc:
                if isinstance(exc, HTTPException): raise
                raise HTTPException(502, detail=f"Groq request failed ({type(exc).__name__}); check model access and provider status.") from exc
            usage = response.get("usage") or {}
            total_input += usage.get("prompt_tokens", 0)
            total_output += usage.get("completion_tokens", 0)
            message = response["choices"][0]["message"]
            calls = message.get("tool_calls") or []
            if not calls:
                answer = (message.get("content") or "").strip()
                if not answer: raise HTTPException(502, detail="Groq returned no final answer.")
                required = _required_for_task(task)
                successful_keys = {row["tool"] for row in outputs if not row["error"]}
                success = required.issubset(successful_keys)
                trace.append({"step": "agent-answer", "message": f"Groq completed in {turn + 1} provider call(s)"})
                return {"success": success, "answer": answer, "tool_calls": calls_used,
                    "tool_outputs": outputs, "trace": trace, "missing_tools": sorted(required-successful_keys),
                    "dependency_failures": [], "reused_entities": [],
                    "agent": {"provider": "Groq", "model": model, "provider_calls": turn+1,
                        "input_tokens": total_input, "output_tokens": total_output,
                        "elapsed_ms": round((time.perf_counter()-started)*1000, 2)}}
            messages.append({"role": "assistant", "content": message.get("content"), "tool_calls": calls})
            for call in calls:
                name = call["function"]["name"]
                tool = by_name.get(name)
                args = None
                if calls_used >= max_calls:
                    result = {"error": f"Tool-call budget ({max_calls}) reached."}
                    errors.append("call_budget")
                else:
                    calls_used += 1
                    try:
                        args = json.loads(call["function"].get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = None
                    if tool is None:
                        result = {"error": "Tool is not in the retrieved HydraDB context."}
                        errors.append("tool_not_retrieved")
                    elif (validation := _validate(tool, args)):
                        result = {"error": validation}
                        errors.append("invalid_arguments")
                    else:
                        blocked = [dep for dep in tool.dependencies if dep not in completed]
                        if blocked:
                            result = {"error": "Required tool dependencies were not executed first: " + ", ".join(blocked)}
                            errors.append("missing_dependency")
                        elif required_order_id and args.get("order_id") and args["order_id"] != required_order_id:
                            result = {"error": "Requested order ID differs from the task's resolved order."}
                            errors.append("entity_mismatch")
                        else:
                            result = execute_data_tool(tool.key, args, data_path)
                            if "error" in result:
                                errors.append("data_miss")
                            else:
                                completed.add(tool.key)
                key = name.replace("__", ".")
                is_error = "error" in result
                outputs.append({"tool": key, "arguments": args, "result": result, "error": is_error})
                trace.append({"step": key, "message": "Groq-selected tool call" if not is_error else "Tool call rejected", "arguments": args, "result": result})
                messages.append({"role": "tool", "tool_call_id": call["id"], "name": name, "content": json.dumps(result, ensure_ascii=False)})
        raise HTTPException(502, detail=f"Groq agent reached the {max_turns}-call iteration limit.")
    finally:
        await client.aclose()


def _required_for_task(task: str) -> set[str]:
    from .dataset_workflow import required_tools
    return set(required_tools(task))
