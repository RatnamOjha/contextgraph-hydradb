#!/usr/bin/env python3
"""Repeatable local comparison against the live ContextGraph API."""
from __future__ import annotations

import json
import statistics
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000"
TASKS = [
    "Ticket T-2049: check refund eligibility from the support case. Do not issue a refund.",
    "Customer C-1042 has a disputed charge; find the invoice and payment history.",
    "Ticket T-2049 reports a late package; inspect the shipment scan and return status.",
]
METHODS = ("full", "flat", "ablation", "graph")
SIZES = (20, 100, 500)
REPETITIONS = 5


def request(path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"}, method="POST" if payload is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{path}: HTTP {exc.code}: {exc.read().decode()}") from exc


def percentile(values: list[float], percent: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    index = (len(values) - 1) * percent
    lower = int(index)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (index - lower)


def main() -> None:
    results = {"methodology": "same three fixed tasks; five repetitions per task; one local HydraDB; no network LLM; 4 strategies; server timings", "runs": [], "learning": {}}
    for size in SIZES:
        print(f"\nSeeding {size}-tool catalog…", flush=True)
        seeded = request("/api/seed", {"catalog_size": size, "reset": True})
        learning = request("/api/evaluate", {"catalog_size": size, "iterations": 3})
        results["learning"][str(size)] = learning["history"]
        print(f"seed={seeded['tools']} tools, held-out accuracy {learning['history'][0]['accuracy']:.3f} → {learning['history'][-1]['accuracy']:.3f}", flush=True)
        per_method: dict[str, list[dict]] = {method: [] for method in METHODS}
        for repetition in range(REPETITIONS):
            for task_index, task in enumerate(TASKS):
                for method in METHODS:
                    row = request("/api/run", {"task": task, "method": method, "catalog_size": size})
                    record = {"repetition": repetition, "task_index": task_index, **row["metrics"], "selected_count": len(row["selected_tools"]), "skill": row.get("skill")}
                    per_method[method].append(record)
        for method, runs in per_method.items():
            retrieval = [float(x["retrieval_ms"]) for x in runs]
            e2e = [float(x["end_to_end_ms"]) for x in runs]
            result = {
                "catalog_size": size,
                "method": method,
                "task_success_rate": sum(bool(x["task_success"]) for x in runs) / len(runs),
                "mean_context_tokens_est": statistics.mean(x["context_tokens_est"] for x in runs),
                "mean_tool_recall": statistics.mean(x["tool_recall"] for x in runs),
                "mean_unnecessary_tools": statistics.mean(x["unnecessary_tools"] for x in runs),
                "retrieval_p50_ms": percentile(retrieval, .50),
                "retrieval_p95_ms": percentile(retrieval, .95),
                "end_to_end_p50_ms": percentile(e2e, .50),
                "end_to_end_p95_ms": percentile(e2e, .95),
                "sample_count": len(runs),
                "runs": runs,
            }
            results["runs"].append(result)
            print(f"{size:>3} {method:>9} | success {result['task_success_rate']:.2f} | ctx {result['mean_context_tokens_est']:.0f} tok | recall {result['mean_tool_recall']:.2f} | retrieval p50/p95 {result['retrieval_p50_ms']:.1f}/{result['retrieval_p95_ms']:.1f} ms | e2e p50/p95 {result['end_to_end_p50_ms']:.1f}/{result['end_to_end_p95_ms']:.1f} ms", flush=True)
    output = Path("results")
    output.mkdir(exist_ok=True)
    path = output / "latest.json"
    path.write_text(json.dumps(results, indent=2) + "\n")
    print(f"\nSaved raw results to {path}")


if __name__ == "__main__":
    main()
