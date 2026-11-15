"""Olist-backed request parsing and deterministic workflow execution."""
from __future__ import annotations

import re
from typing import Any

from .fixtures import DATA_TOOLS
from .olist_store import execute_data_tool, lookup_order
from .router import candidate_skills

TOOL_BY_KEY = {tool.key: tool for tool in DATA_TOOLS}


def extract_order_id(task: str) -> tuple[str | None, str | None]:
    ids = re.findall(r"\b[0-9a-f]{32}\b", task, re.I)
    if len(set(x.lower() for x in ids)) > 1:
        return None, "The request contains multiple record IDs. Please select one order at a time."
    if ids:
        return ids[0].lower(), None
    return None, None


def required_tools(task: str) -> list[str]:
    lower = task.lower()
    candidates = {row["skill"] for row in candidate_skills(task)}
    required = ["order.details"]
    if "customer_context" in candidates and any(w in lower for w in ("customer", "buyer", "shopper", "account", "their other", "previous orders")):
        required.extend(["customer.lookup", "customer.orders"])
    if any(w in lower for w in ("item", "items", "line", "product", "seller", "category", "weight", "goods", "purchased", "bought")):
        required.append("order.items")
    if "billing_review" in candidates:
        required.append("order.payments")
    if "delivery_investigation" in candidates:
        required.append("order.delivery_status")
    if "refund_review" in candidates:
        required.extend(["policy.lookup", "refund.eligibility"])
    if "product_research" in candidates and any(w in lower for w in ("product details", "seller details", "product category", "seller state")):
        required.extend(["product.details", "seller.details"])
    return list(dict.fromkeys(required))


def execute_deterministic(task: str, order_id: str, selected: list[Any], data_path=None) -> dict[str, Any]:
    allowed = {tool.key: tool for tool in selected}
    plan = required_tools(task)
    results: list[dict[str, Any]] = []
    trace: list[dict[str, Any]] = []
    completed: set[str] = set()
    missing: list[str] = []
    dependency_failures: list[dict[str, str]] = []
    order_data = None
    customer_data = None
    for key in plan:
        tool = allowed.get(key)
        if tool is None:
            missing.append(key)
            trace.append({"step": key, "message": "Required tool was not included in the retrieved context."})
            continue
        blocked = [dep for dep in tool.dependencies if dep not in completed]
        if blocked:
            dependency_failures.extend({"tool": key, "missing_dependency": dep} for dep in blocked)
            trace.append({"step": key, "message": "Blocked by missing dependency: " + ", ".join(blocked)})
            continue
        args: dict[str, Any]
        if key in {"order.details", "order.items", "order.payments", "order.delivery_status"}:
            args = {"order_id": order_id}
        elif key == "customer.lookup":
            args = {"customer_id": (order_data or {}).get("customer_id", "")}
        elif key == "customer.orders":
            args = {"customer_unique_id": (customer_data or {}).get("customer_unique_id", "")}
        elif key == "policy.lookup":
            args = {"topic": "refund"}
        elif key == "refund.eligibility":
            args = {"order_id": order_id, "reason": "support inquiry"}
        elif key == "product.details":
            first = next((row["product_id"] for output in results if output["tool"] == "order.items" for row in output["result"].get("data", [])), "")
            args = {"product_id": first}
        elif key == "seller.details":
            first = next((row["seller_id"] for output in results if output["tool"] == "order.items" for row in output["result"].get("data", [])), "")
            args = {"seller_id": first}
        else:
            missing.append(key); continue
        result = execute_data_tool(key, args, data_path)
        is_error = "error" in result
        results.append({"tool": key, "arguments": args, "result": result, "error": is_error})
        trace.append({"step": key, "message": "Executed against imported Olist records" if not is_error else "Dataset tool failed", "arguments": args, "result": result})
        if is_error:
            missing.append(key)
        else:
            completed.add(key)
            if key == "order.details": order_data = result.get("data")
            if key == "customer.lookup": customer_data = result.get("data")
    success = not missing and not dependency_failures and len(completed) == len(plan)
    facts = []
    for output in results:
        if output["error"]: continue
        payload = output["result"].get("data")
        if output["tool"] == "order.details" and payload:
            facts.append(f"source status {payload['status']}, {payload['item_count']} line items, total BRL {payload['total']:.2f}")
        elif output["tool"] == "order.delivery_status" and payload:
            if payload.get("delivered_at") and payload.get("estimated_at"):
                timing = "delivered late" if payload["delivery_outcome"] == "delivered_late" else "delivered on time"
                facts.append(f"delivery timestamps: actual {payload['delivered_at']}; estimated {payload['estimated_at']} ({timing})")
            else:
                facts.append("delivery timing unverified: source timestamps do not establish both actual and estimated delivery; a missing actual timestamp does not prove non-delivery")
        elif output["tool"] == "order.payments":
            rows = payload or []
            total_paid = sum(float(row["value"]) for row in rows)
            kinds = ", ".join(sorted({row["kind"] for row in rows})) or "none"
            facts.append(f"{len(rows)} source payment record(s), BRL {total_paid:.2f} total, type(s) {kinds}")
        elif output["tool"] == "refund.eligibility" and payload:
            facts.append(f"simulated demo refund result: {payload['eligible_under_demo_policy']}")
        elif output["tool"] == "order.items":
            rows = payload or []
            categories = ", ".join(sorted({row["category"] for row in rows if row.get("category")})) or "not available"
            facts.append(f"{len(rows)} source item record(s), categories: {categories}")
    answer = (f"Order {order_id}: " + "; ".join(facts) + ". Historical amounts are in BRL. "
              "Refund eligibility, when shown, uses a simulated demo rule rather than an Olist policy.") if success else "The workflow could not complete because retrieved context or source data was missing. See the execution trace."
    return {"success": success, "answer": answer, "tool_calls": len(results), "tool_outputs": results, "trace": trace,
        "missing_tools": sorted(set(missing)), "dependency_failures": dependency_failures,
        "reused_entities": []}
