"""Deterministic objective extraction and evidence-backed answer scoring."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any


def requested_objectives(task: str) -> list[dict[str, str]]:
    text = task.lower()
    out: list[dict[str, str]] = []
    def add(key: str, label: str) -> None:
        out.append({"id": key, "label": label})

    if re.search(r"\b(status|state)\b", text): add("status", "Order status")
    has_payment = bool(re.search(r"\b(payment|payments|paid|charge|transaction|installment|money trail)\b", text))
    if has_payment and re.search(r"\b(total|amount|value|reconcile|money)\b", text): add("payment_total", "Payment total")
    if has_payment and re.search(r"\b(method|type|kind|payment types)\b", text): add("payment_methods", "Payment method")
    if has_payment and re.search(r"\b(installment|installments)\b", text): add("installments", "Installments")
    if re.search(r"\b(total|order value)\b", text) and not has_payment: add("item_total", "Item total (price + freight)")
    if re.search(r"\b(delivery|delivered|shipment|shipping|carrier|parcel)\b", text) and re.search(r"\b(time|timing|date|late|promised|arrive|arrived|timestamp|timeline|when)\b", text):
        add("delivery_timing", "Delivery timing")
    if re.search(r"\b(line items?|items|goods|products|category|categories|seller)\b", text): add("items", "Line items")
    if re.search(r"\bseller(?: ids?| identifiers?)\b", text): add("seller_ids", "Seller IDs")
    if re.search(r"\b(customer|buyer|shopper)\b", text) and re.search(r"\b(history|other orders|previous orders|orders)\b", text): add("customer_orders", "Customer order history")
    if re.search(r"\b(purchase date|purchased on|when was this order placed)\b", text): add("purchase_date", "Purchase date")
    if re.search(r"\b(refund|eligible|eligibility|reimburse)\b", text) and not re.search(r"\b(policy|rule)\b", text): add("refund_eligibility", "Simulated refund eligibility")
    return out


def evidence_from_outputs(outputs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    facts: dict[str, dict[str, Any]] = {}
    by_tool = {r.get("tool"): r.get("result", {}).get("data") for r in outputs
               if not r.get("error") and isinstance(r.get("result"), dict) and "error" not in r.get("result", {})}
    details = by_tool.get("order.details")
    delivery = by_tool.get("order.delivery_status")
    payments = by_tool.get("order.payments")
    if isinstance(details, dict):
        facts["status"] = {"value": details.get("status"), "tool": "order.details", "sufficient": bool(details.get("status")), "source": "Olist order status"}
        facts["purchase_date"] = {"value": details.get("purchase_at"), "tool": "order.details", "sufficient": bool(details.get("purchase_at")), "source": "Olist purchase timestamp"}
        facts["item_total"] = {"value": round(float(details["total"]), 2) if details.get("total") is not None else None,
            "tool": "order.details", "sufficient": details.get("total") is not None, "source": "Sum of item price and freight; not payment total"}
    if isinstance(delivery, dict):
        if delivery.get("source_status"):
            facts.setdefault("status", {"value": delivery["source_status"], "tool": "order.delivery_status", "sufficient": True, "source": "Olist order timestamps"})
        actual, estimated = delivery.get("delivered_at"), delivery.get("estimated_at")
        facts["delivery_timing"] = {"value": None, "actual": actual, "estimated": estimated,
            "outcome": delivery.get("delivery_outcome") if actual and estimated else None,
            "tool": "order.delivery_status", "sufficient": bool(actual and estimated),
            "source": "Olist delivery and estimated timestamps"}
    if isinstance(payments, list):
        values = [float(row["value"]) for row in payments if isinstance(row, dict) and row.get("value") is not None]
        kinds = sorted({str(row["kind"]) for row in payments if isinstance(row, dict) and row.get("kind")})
        installments = sorted({int(row["installments"]) for row in payments if isinstance(row, dict) and row.get("installments") is not None})
        facts["payment_total"] = {"value": round(sum(values), 2), "tool": "order.payments", "sufficient": bool(payments), "source": "Sum of Olist payment records"}
        facts["payment_methods"] = {"value": kinds, "tool": "order.payments", "sufficient": bool(kinds), "source": "Olist payment records"}
        facts["installments"] = {"value": installments, "tool": "order.payments", "sufficient": bool(installments), "source": "Olist payment records"}
    items = by_tool.get("order.items")
    if isinstance(items, list):
        facts["items"] = {"value": items, "tool": "order.items", "sufficient": bool(items) or items == [], "source": "Olist line item records"}
        facts["seller_ids"] = {"value": sorted({str(row["seller_id"]) for row in items if row.get("seller_id")}),
            "tool": "order.items", "sufficient": True, "source": "Olist line item records"}
    customer_orders = by_tool.get("customer.orders")
    if isinstance(customer_orders, list):
        facts["customer_orders"] = {"value": customer_orders, "tool": "customer.orders", "sufficient": True, "source": "Olist customer order records"}
    refund = by_tool.get("refund.eligibility")
    if isinstance(refund, dict):
        facts["refund_eligibility"] = {"value": refund.get("eligible_under_demo_policy"), "tool": "refund.eligibility",
            "sufficient": refund.get("eligible_under_demo_policy") is not None, "source": "Simulated ContextGraph demo policy",
            "simulated": True, "caveat": refund.get("caveat")}
    return facts


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().replace("_", " ")).strip()


def _amounts(text: str) -> list[Decimal]:
    # Only capture amounts with a currency marker, avoiding arbitrary IDs/counts.
    pattern = r"(?:R\$|BRL|\$)\s*([0-9]{1,3}(?:[.,][0-9]{3})*(?:[.,][0-9]{2})|[0-9]+(?:[.,][0-9]{1,2})?)|([0-9]{1,3}(?:[.,][0-9]{3})*(?:[.,][0-9]{2})|[0-9]+(?:[.,][0-9]{1,2})?)\s*(?:BRL|reais)"
    values = []
    for match in re.finditer(pattern, text, re.I):
        try: values.append(_decimal_amount(match.group(1) or match.group(2) or ""))
        except InvalidOperation: pass
    return values


def _decimal_amount(raw: str) -> Decimal:
    raw = raw.replace(" ", "")
    if "," in raw and "." in raw:
        decimal_mark = "," if raw.rfind(",") > raw.rfind(".") else "."
        thousands_mark = "." if decimal_mark == "," else ","
        raw = raw.replace(thousands_mark, "").replace(decimal_mark, ".")
    elif "," in raw:
        left, right = raw.rsplit(",", 1)
        raw = (left.replace(",", "") + ("." + right if len(right) <= 2 else right))
    elif raw.count(".") > 1:
        left, right = raw.rsplit(".", 1)
        raw = left.replace(".", "") + ("." + right if len(right) <= 2 else right)
    return Decimal(raw).quantize(Decimal("0.01"))


def _answers_objective(key: str, answer: str, fact: dict[str, Any] | None) -> tuple[bool, bool | None, Any, str]:
    if not fact or not fact.get("sufficient"):
        return False, None, None, "No sufficient source evidence was returned."
    text = _norm(answer)
    value = fact.get("value")
    if key == "status":
        if not value: return False, None, None, "Source status is missing."
        status = _norm(str(value))
        known = {"approved", "created", "invoiced", "processing", "shipped", "delivered", "canceled", "unavailable", "unknown"}
        explicit = [s for s in known if re.search(rf"\b(?:status|state)(?:\s+is|\s+of|\s*:)?\s+(?:currently\s+)?{re.escape(s)}\b", text)
                    or re.search(rf"\b(?:order|it)\s+(?:is|was|remains)\s+(?:currently\s+)?{re.escape(s)}\b", text)]
        mentioned = explicit or [s for s in known if re.search(rf"\b{re.escape(s)}\b", text)]
        if "delivered" in mentioned and re.search(r"\bnot delivered\b", text): mentioned.remove("delivered")
        correct = status in mentioned if mentioned else None
        return bool(mentioned), correct, mentioned[0] if mentioned else None, "Checked against the source order status."
    if key == "payment_total":
        amount_values = _amounts(answer)
        expected = Decimal(str(value)).quantize(Decimal("0.01"))
        relevant = bool(re.search(r"\b(payment|paid|charge|transaction)\b", text)) and bool(amount_values)
        if not relevant: return False, None, None, "Answer did not state a currency amount as the payment total."
        correct = expected in amount_values
        return True, correct, [str(x) for x in amount_values], "Compared the stated currency amount with the sum of source payment rows."
    if key == "payment_methods":
        methods = value or []
        mentions = []
        for method in methods:
            pattern = r"\b" + re.escape(_norm(method)).replace(r"\ ", r"\s+") + r"\b"
            if re.search(pattern, text): mentions.append(method)
        if not mentions: return False, None, None, "Answer did not name a source payment method."
        return True, set(map(_norm, mentions)) == set(map(_norm, methods)), mentions, "Compared named methods with source payment rows."
    if key == "installments":
        expected = set(value or [])
        nums = {int(x) for x in re.findall(r"\b(\d{1,2})\s*(?:installments?|payments?)\b", text)}
        if not nums: return False, None, None, "Answer did not state an installment count."
        return True, nums == expected, sorted(nums), "Compared installment counts with source payment rows."
    if key == "delivery_timing":
        outcome = fact.get("outcome")
        if not outcome: return False, None, None, "Source timestamps do not establish both actual and estimated delivery; no delivery outcome was inferred."
        expected = "delivered late" if outcome == "delivered_late" else "delivered on time"
        if outcome == "not_delivered_in_source": return False, None, None, "Actual delivery timestamp is absent; absence alone does not establish non-delivery."
        late = bool(re.search(r"\b(late|after the promised|after the estimated)\b", text))
        ontime = bool(re.search(r"\b(on time|before the promised|by the promised|by the estimated|ahead of schedule)\b", text))
        actual = fact.get("actual", "")[:10]
        estimated = fact.get("estimated", "")[:10]
        date_mentioned = actual in answer or estimated in answer if actual and estimated else False
        if not (late or ontime or date_mentioned): return False, None, None, "Answer did not state a verifiable delivery date or timing comparison."
        correct = (late and outcome == "delivered_late") or (ontime and outcome == "delivered_on_time") or (date_mentioned and actual in answer and (late or ontime))
        return True, correct, expected if late or ontime else {"actual": actual, "estimated": estimated}, "Compared answer with source actual and estimated delivery timestamps."
    if key == "purchase_date":
        date = str(value or "")[:10]
        return bool(date and date in answer), bool(date and date in answer), date if date in answer else None, "Compared with the source purchase timestamp."
    if key == "item_total":
        amount_values = _amounts(answer)
        expected = Decimal(str(value)).quantize(Decimal("0.01"))
        if not amount_values: return False, None, None, "Answer did not state an item total with a currency value."
        return True, expected in amount_values, [str(x) for x in amount_values], "Compared with source item price plus freight; this is not the payment total."
    if key == "refund_eligibility":
        claim = "not eligible" if re.search(r"\bnot eligible|ineligible\b", text) else "eligible" if re.search(r"\beligible\b", text) else None
        if claim is None: return False, None, None, "Answer did not state the simulated eligibility result."
        expected = "eligible" if bool(value) else "not eligible"
        return True, claim == expected, claim, "Compared with simulated ContextGraph policy output; not an Olist fact."
    if key == "items":
        rows = value or []
        ids = {str(row.get("product_id")) for row in rows if row.get("product_id")}
        ids_mentioned = {i for i in ids if i in answer}
        categories = {str(row.get("category")) for row in rows if row.get("category") and row.get("category") != "Unknown"}
        cats_mentioned = {c for c in categories if _norm(c) in text}
        answered = bool(ids_mentioned or cats_mentioned or re.search(r"\b\d+\s+(?:line )?items?\b", text))
        if ids_mentioned: correct = ids_mentioned == ids
        elif cats_mentioned: correct = cats_mentioned == categories
        else:
            count_match = re.search(r"\b(\d+)\s+(?:line )?items?\b", text)
            correct = bool(count_match and int(count_match.group(1)) == len(rows))
        return answered, correct if answered else None, {"product_ids": sorted(ids_mentioned), "categories": sorted(cats_mentioned)}, "Compared named product IDs/categories with source line items."
    if key == "seller_ids":
        expected_ids = set(value or [])
        mentioned = {seller for seller in expected_ids if seller in answer}
        return bool(mentioned), mentioned == expected_ids if mentioned else None, sorted(mentioned), "Compared seller IDs with source line-item records."
    if key == "customer_orders":
        rows = value or []
        count = len(rows)
        found = re.search(r"\b(\d+)\s+(?:orders?|purchases?)\b", text)
        if not found: return False, None, None, "Answer did not state the number of matching customer orders."
        return True, int(found.group(1)) == count, int(found.group(1)), "Compared count with source customer order records."
    return False, None, None, "No deterministic verifier exists for this objective."


def evaluate_answer(task: str, answer: str, outputs: list[dict[str, Any]], *, entity_resolved: bool = True,
                    retrieval_ms: float | None = None, context_tokens: int | None = None) -> dict[str, Any]:
    objectives = requested_objectives(task)
    facts = evidence_from_outputs(outputs)
    scored = []
    for objective in objectives:
        fact = facts.get(objective["id"])
        answered, correct, answer_value, reason = _answers_objective(objective["id"], answer, fact)
        scored.append({**objective, "answered": answered, "factually_correct": correct,
            "evidence_sufficient": bool(fact and fact.get("sufficient")), "supporting_tool": fact.get("tool") if fact else None,
            "evidence_source": fact.get("source") if fact else None,
            "expected_value": fact.get("value") if fact and objective["id"] not in {"items", "seller_ids", "customer_orders"} else None,
            "answer_value": answer_value, "reason": reason})
    unsupported = []
    amounts = _amounts(answer)
    supported_amounts = {Decimal(str(f["value"])).quantize(Decimal("0.01")) for f in facts.values() if f.get("value") is not None and isinstance(f.get("value"), (int,float))}
    for match in re.finditer(r"(?:R\$|BRL|\$)\s*([0-9,]+(?:\.[0-9]{1,2})?)|([0-9,]+(?:\.[0-9]{1,2})?)\s*(?:BRL|reais)", answer, re.I):
        try: amount = _decimal_amount(match.group(1) or match.group(2) or "")
        except InvalidOperation: continue
        context = answer[max(0, match.start()-45):min(len(answer), match.end()+25)].lower()
        payment_claim = bool(re.search(r"payment|paid|charge|transaction", context))
        payment_fact = facts.get("payment_total")
        supported = (payment_fact and payment_fact.get("sufficient") and amount == Decimal(str(payment_fact["value"])).quantize(Decimal("0.01"))) if payment_claim else amount in supported_amounts
        if not supported:
            unsupported.append({"type": "currency_amount", "claim": str(amount), "reason": "No returned Olist tool result supports this currency amount in the stated context."})
    status_fact = facts.get("status")
    if status_fact and status_fact.get("sufficient"):
        known = {"approved", "created", "invoiced", "processing", "shipped", "delivered", "canceled", "unavailable", "unknown"}
        normalized_answer = _norm(answer)
        claims = [s for s in known if re.search(rf"\b(?:status|state)(?:\s+is|\s+of|\s*:)?\s+(?:currently\s+)?{re.escape(s)}\b", normalized_answer)
                  or re.search(rf"\b(?:order|it)\s+(?:is|was|remains)\s+(?:currently\s+)?{re.escape(s)}\b", normalized_answer)]
        if claims and _norm(str(status_fact.get("value"))) not in claims:
            unsupported.append({"type": "order_status", "claim": claims, "reason": "The stated status differs from the source order status."})
    delivery_fact = facts.get("delivery_timing")
    if delivery_fact and not delivery_fact.get("sufficient") and re.search(r"\b(not delivered|never delivered|was not delivered)\b", _norm(answer)):
        unsupported.append({"type": "delivery_claim", "claim": "not delivered", "reason": "A missing delivery timestamp does not establish non-delivery."})
    for row in scored:
        if row["id"] == "refund_eligibility" and row["answered"] and (not row["evidence_sufficient"] or row.get("supporting_tool") != "refund.eligibility"):
            unsupported.append({"type": "simulated_policy_claim", "claim": row.get("answer_value"), "reason": "No simulated refund eligibility result supports this claim."})
    all_correct = bool(scored) and all(row["factually_correct"] is True and row["evidence_sufficient"] for row in scored)
    some_correct = any(row["factually_correct"] is True and row["evidence_sufficient"] for row in scored)
    any_wrong = any(row["factually_correct"] is False and row["answered"] for row in scored)
    execution_messages = [str(r.get("result", {}).get("error", "")) for r in outputs if isinstance(r.get("result"), dict)]
    # Tool errors currently use both "dependency" and the plural "dependencies".
    # Match the shared stem so an unmet prerequisite is surfaced as BLOCKED.
    has_dependency_block = any("dependenc" in message.lower() for message in execution_messages)
    if all_correct and not unsupported and entity_resolved: code = "SUCCESS"
    elif any_wrong or unsupported: code = "FAILURE"
    elif not entity_resolved or has_dependency_block: code = "BLOCKED"
    elif some_correct: code = "PARTIAL"
    else: code = "UNVERIFIED"
    verified = sum(row["factually_correct"] is True and row["evidence_sufficient"] for row in scored)
    if code == "SUCCESS": explanation = f"All {len(scored)} requested objectives are supported by source evidence and stated correctly."
    elif code == "PARTIAL": explanation = f"{verified} of {len(scored)} requested objectives are verified; remaining claims are incomplete or unsupported."
    elif code == "FAILURE": explanation = "The answer contains a contradicted or unsupported factual claim."
    elif code == "BLOCKED": explanation = "The task could not proceed because the requested entity or required context was unavailable."
    else: explanation = "The returned evidence is insufficient to verify the requested answer."
    return {"code": code, "explanation": explanation, "verified_objectives": verified,
        "total_objectives": len(scored), "objectives": scored, "unsupported_claims": unsupported,
        "evidence": facts, "structured_summary": _summary(facts)}


def _summary(facts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {key: facts[key] for key in ("status", "item_total", "payment_total", "payment_methods", "delivery_timing") if key in facts}
