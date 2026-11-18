#!/usr/bin/env python3
"""Paired deterministic benchmark for the synthetic support-company catalog.

Run this against a local dev server started with CONTEXTGRAPH_CATALOG_SCOPE=company.
No LLM provider is called; tokens are estimates of serialized tool definitions.
"""
from __future__ import annotations

import argparse
import json
import statistics
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.fixtures import TASKS

METHODS = ("full", "flat", "expanded_flat", "graph")


def request(base_url: str, task: str, method: str) -> dict[str, Any]:
    payload = {"task": task, "method": method, "execution_mode": "fixture", "catalog_scope": "company", "catalog_size": 20}
    req = urllib.request.Request(base_url.rstrip("/") + "/api/run", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=90) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{method} failed with HTTP {exc.code}: {exc.read().decode()}") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, default=Path("results/company_contextgraph_benchmark.json"))
    args = parser.parse_args()

    runs: dict[str, list[dict[str, Any]]] = {method: [] for method in METHODS}
    cases = []
    for scenario, fixture in TASKS.items():
        case = {"scenario": scenario, "task": fixture["task"], "methods": {}}
        for method in METHODS:
            result = request(args.base_url, fixture["task"], method)
            outputs = result.get("tool_outputs", [])
            required = set(fixture["required"])
            row = {
                "success": bool(result["metrics"]["task_success"]),
                "tool_context_tokens_est": int(result["metrics"]["context_tokens_est"]),
                "unnecessary_tools_exposed": int(result["metrics"]["unnecessary_tools"]),
                "tool_recall": float(result["metrics"]["tool_recall"]),
                "executed_tool_calls": int(result["metrics"]["tool_calls"]),
                "unnecessary_executed_calls": sum(item.get("tool") not in required for item in outputs),
                "selected_tools": [tool["key"] for tool in result.get("selected_tools", [])],
                "executed_tools": [item.get("tool") for item in outputs],
                "resolved_entities": result.get("resolved_entities", []),
            }
            case["methods"][method] = row
            runs[method].append(row)
        cases.append(case)

    summary = {}
    for method, rows in runs.items():
        summary[method] = {
            "completion": f"{sum(row['success'] for row in rows)}/{len(rows)}",
            "mean_tool_context_tokens_est": round(statistics.mean(row["tool_context_tokens_est"] for row in rows), 1),
            "mean_unnecessary_tools_exposed": round(statistics.mean(row["unnecessary_tools_exposed"] for row in rows), 1),
            "mean_executed_tool_calls": round(statistics.mean(row["executed_tool_calls"] for row in rows), 2),
            "total_unnecessary_executed_calls": sum(row["unnecessary_executed_calls"] for row in rows),
            "mean_tool_recall": round(statistics.mean(row["tool_recall"] for row in rows), 3),
        }
    baseline, graph = summary["full"], summary["graph"]
    baseline_tokens, graph_tokens = baseline["mean_tool_context_tokens_est"], graph["mean_tool_context_tokens_est"]
    baseline_calls, graph_calls = baseline["mean_executed_tool_calls"], graph["mean_executed_tool_calls"]
    report = {
        "methodology": {
            "company": "Fictional Acme Support: Support Desk, Commerce, Billing, and Shipping MCP catalogs",
            "catalog_scope": "company only; 18 synthetic MCP tool definitions; no Olist tools",
            "cases": list(TASKS),
            "execution": "deterministic fixture mode; same three tasks for every retrieval method",
            "token_measurement": "estimated tokens from serialized selected tool definitions using the app's UTF-8 JSON character-count / 4 approximation; excludes system/user prompts, results, and generated answers",
            "unnecessary_call_measurement": "counts any actually executed fixture tool not in that task's declared required-tool list",
            "provider_calls": 0,
        },
        "summary": summary,
        "full_vs_graph": {
            "mean_tool_context_token_reduction_pct": round((baseline_tokens - graph_tokens) / baseline_tokens * 100, 1) if baseline_tokens else None,
            "mean_executed_call_reduction_pct": round((baseline_calls - graph_calls) / baseline_calls * 100, 1) if baseline_calls else None,
            "task_completion_preserved": baseline["completion"] == graph["completion"],
        },
        "cases": cases,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"summary": summary, "full_vs_graph": report["full_vs_graph"], "output": str(args.output)}, indent=2))


if __name__ == "__main__":
    main()
