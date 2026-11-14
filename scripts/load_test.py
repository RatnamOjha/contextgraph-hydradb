#!/usr/bin/env python3
"""Concurrent live requests against the HydraDB-backed graph route."""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time

import httpx

TASKS = [
    "Ticket T-2049: check refund eligibility; do not issue a refund.",
    "Customer C-1042 has a disputed charge; inspect invoice and payment history.",
    "Ticket T-2049 has a late package; check shipment scan and return status.",
]


def percentile(values: list[float], p: float) -> float:
    values = sorted(values)
    if not values:
        return 0.0
    at = (len(values) - 1) * p
    lo = int(at); hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (at - lo)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests", type=int, default=30)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--size", type=int, choices=(20, 100, 500), default=100)
    args = parser.parse_args()
    sem = asyncio.Semaphore(args.concurrency)
    timings: list[float] = []
    errors: list[str] = []
    successes = 0
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8000", timeout=30) as client:
        async def one(i: int) -> None:
            nonlocal successes
            async with sem:
                started = time.perf_counter()
                try:
                    response = await client.post("/api/run", json={"task": TASKS[i % len(TASKS)], "method": "graph", "catalog_size": args.size})
                    response.raise_for_status()
                    result = response.json()
                    timings.append((time.perf_counter() - started) * 1000)
                    successes += bool(result["metrics"]["task_success"] and result["metrics"]["dependency_success"])
                except Exception as exc:
                    errors.append(str(exc))
        await asyncio.gather(*(one(i) for i in range(args.requests)))
    print(json.dumps({
        "requests": args.requests,
        "concurrency": args.concurrency,
        "catalog_size": args.size,
        "successful_workflows": successes,
        "errors": len(errors),
        "request_p50_ms": round(percentile(timings, .50), 3),
        "request_p95_ms": round(percentile(timings, .95), 3),
        "request_mean_ms": round(statistics.mean(timings), 3) if timings else None,
        "first_errors": errors[:3],
    }, indent=2))
    if errors or successes != args.requests:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
