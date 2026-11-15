"""Olist-backed request parsing and deterministic workflow execution."""
from __future__ import annotations

import re
from typing import Any

from .fixtures import DATA_TOOLS
from .olist_store import execute_data_tool, lookup_order
from .objectives import evidence_from_outputs, requested_objectives
from .router import candidate_skills

TOOL_BY_KEY = {tool.key: tool for tool in DATA_TOOLS}


def _kind_label(value: Any) -> str:
    return str(value).replace("_", " ") if value else "method unavailable"


def _grounded_answer(task: str, order_id: str, outputs: list[dict[str, Any]]) -> str:
    """Compose a fixture answer from returned rows, stating each recognized objective."""
    facts = evidence_from_outputs(outputs)
    by_tool = {row.get("tool"): row.get("result", {}).get("data") for row in outputs
               if not row.get("error") and isinstance(row.get("result"), dict)
               and "error" not in row.get("result", {})}
    parts: list[str] = []
    payment_summary_added = False
    payment_total_added = False
    for objective in requested_objectives(task):
        key = objective["id"]
        fact = facts.get(key)
        if key == "status":
            parts.append(f"Order status: {fact['value']}." if fact and fact.get("sufficient") else "Order status is unavailable in the returned source data.")
        elif key == "payment_total":
            if fact and fact.get("sufficient"):
                parts.append(f"Payment total: BRL {fact['value']:.2f} across the source payment records.")
            else:
                parts.append("Payment total is unverified because one or more payment values are missing from the source records.")
            payment_total_added = True
        elif key == "payment_methods" or key == "installments":
            if payment_summary_added:
                continue
            payment_summary_added = True
            rows = by_tool.get("order.payments")
            if not isinstance(rows, list):
                parts.append("Payment records were not returned, so methods, values, and installments are unverified.")
                continue
            valid_rows = [row for row in rows if isinstance(row, dict)]
            count = len(valid_rows)
            if count == 1:
                parts.append("There is 1 payment record.")
            else:
                parts.append(f"There are {count} payment records.")
            kinds = sorted({str(row["kind"]) for row in valid_rows if row.get("kind")})
            if len(kinds) == 1:
                parts.append(f"Only one payment method is present: {_kind_label(kinds[0])}.")
            elif len(kinds) > 1:
                parts.append("Multiple payment methods are present: " + ", ".join(_kind_label(value) for value in kinds) + ".")
            else:
                parts.append("Payment method values are missing from the source records.")
            payment_total = facts.get("payment_total")
            if payment_total and payment_total.get("sufficient") and not payment_total_added:
                parts.append(f"Payment total: BRL {payment_total['value']:.2f} across the source payment records.")
            elif any(obj["id"] == "payment_total" for obj in requested_objectives(task)):
                if not payment_total_added:
                    parts.append("Payment total is unverified because one or more payment values are missing from the source records.")
            for index, row in enumerate(valid_rows, 1):
                method = _kind_label(row.get("kind"))
                value = f"BRL {float(row['value']):.2f}" if row.get("value") is not None else "payment value unavailable"
                installment_count = row.get("installments")
                installment_text = (f"{int(installment_count)} installment" + ("" if int(installment_count) == 1 else "s")
                                    if installment_count is not None else "installment count unavailable")
                parts.append(f"Payment record {index}: method {method}, payment value {value}, {installment_text}.")
            if key == "installments" and not count:
                parts.append("No source payment records were returned; installment count is unverified.")
        elif key == "items":
            rows = by_tool.get("order.items")
            if isinstance(rows, list):
                categories = sorted({str(row["category"]) for row in rows if row.get("category") and row["category"] != "Unknown"})
                if not rows:
                    parts.append("The source contains 0 line items.")
                else:
                    parts.append(f"The source contains {len(rows)} line items" + (f" across categories: {', '.join(categories)}." if categories else "."))
                    ids = [str(row["product_id"]) for row in rows if row.get("product_id")]
                    if ids:
                        parts.append("Product IDs: " + ", ".join(ids) + ".")
            else:
                parts.append("Line items were not returned, so the item list is unverified.")
        elif key == "seller_ids":
            rows = by_tool.get("order.items")
            sellers = sorted({str(row["seller_id"]) for row in rows or [] if isinstance(row, dict) and row.get("seller_id")}) if isinstance(rows, list) else []
            parts.append("Seller IDs: " + (", ".join(sellers) if sellers else "none returned") + ".")
        elif key == "delivery_timing":
            delivery = by_tool.get("order.delivery_status")
            if isinstance(delivery, dict):
                actual, estimated = delivery.get("delivered_at"), delivery.get("estimated_at")
                if actual and estimated:
                    timing = "delivered late" if delivery.get("delivery_outcome") == "delivered_late" else "delivered on time"
                    parts.append(f"Delivery timestamps: actual {actual}; estimated {estimated}; {timing}.")
                else:
                    missing = "actual" if not actual else "estimated"
                    parts.append(f"Delivery timing is unverified: the source {missing} timestamp is missing; absence does not establish non-delivery.")
            else:
                parts.append("Delivery timestamps were not returned, so delivery timing is unverified.")
        elif key == "purchase_date":
            parts.append(f"Purchase timestamp: {fact['value']}." if fact and fact.get("sufficient") else "Purchase timestamp is unavailable in the returned source data.")
        elif key == "item_total":
            parts.append(f"Item total (price plus freight): BRL {fact['value']:.2f}." if fact and fact.get("sufficient") else "Item total is unavailable in the returned source data.")
        elif key == "customer_orders":
            rows = fact.get("value", []) if fact else []
            parts.append(f"Matching customer order history: {len(rows)} orders.")
        elif key == "refund_eligibility":
            if fact and fact.get("sufficient"):
                parts.append(f"Simulated demo refund eligibility: {'eligible' if fact['value'] else 'not eligible'}; {fact.get('caveat', 'this is a demo policy result, not an Olist fact')}.")
            else:
                parts.append("Simulated refund eligibility is unavailable.")
    if not parts:
        parts.append("No recognized factual objective was found for this request; see the execution trace for returned source context.")
    return f"Order {order_id}. " + " ".join(parts)


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
    answer = _grounded_answer(task, order_id, results)
    return {"success": success, "answer": answer, "tool_calls": len(results), "tool_outputs": results, "trace": trace,
        "missing_tools": sorted(set(missing)), "dependency_failures": dependency_failures,
        "reused_entities": []}
