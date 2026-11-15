"""Small deterministic Olist evaluation; real Groq runs are opt-in and separate."""
from __future__ import annotations

import json
from typing import Any

from .fixtures import DATA_TOOLS
from .dataset_workflow import execute_deterministic
from .olist_store import list_orders
from .router import candidate_skills, flat_candidates, route_graph_candidates, tool_json

CASES = [
    ("order_summary", "Summarize the status, purchase date, and total for order {id}.", {"order.details"}),
    ("line_items", "Which line items, categories, and seller IDs were included in historical record {id}?", {"order.details", "order.items"}),
    ("payment_reconcile", "Reconcile the payment types, installment count, and amounts for order {id}.", {"order.details", "order.payments"}),
    ("delivery_timing", "Did parcel {id} arrive by the promised date? Compare its tracking timestamps.", {"order.details", "order.delivery_status"}),
    ("items_and_delivery", "Show the goods in historical record {id} and whether delivery was late.", {"order.details", "order.items", "order.delivery_status"}),
    ("simulated_refund", "For order {id}, apply the ContextGraph demo refund rule and explain the result.", {"order.details", "policy.lookup", "refund.eligibility"}),
    ("unfamiliar_payment", "I need the money trail for order {id}: amounts, method, and installments.", {"order.details", "order.payments"}),
    ("overlapping_item_charge", "For historical record {id}, compare item price and freight against the recorded transactions.", {"order.details", "order.items", "order.payments"}),
    ("product_seller", "Identify the product category and seller state for goods in historical record {id}.", {"order.details", "order.items", "product.details", "seller.details"}),
]


async def evaluate_olist(db, case_count: int = 9) -> dict[str, Any]:
    # Rows 0–1 are reserved for the learning demonstration; evaluation uses
    # disjoint source order IDs and fixed prompts/required-tool annotations.
    orders = list_orders(200)
    heldout = orders[2:2 + min(case_count, len(CASES))]
    if len(heldout) < 4: raise RuntimeError("Need at least six imported orders for disjoint training and held-out evaluation.")
    catalog = list(DATA_TOOLS)
    learned_edges: set[tuple[str, str]] = set()
    try:
        _, rows, _ = await db.query("MATCH (u:LearningUpdate {state: 'applied'}) RETURN u.tool AS tool, u.dependency AS dependency")
        learned_edges = {(a, b) for a, b in rows if a and b}
    except Exception:
        learned_edges = set()
    case_results = []
    method_totals: dict[str, list[dict[str, Any]]] = {name: [] for name in (
        "full_context", "flat_lexical", "flat_dependency_expanded", "semantic_dependency_expanded",
        "graph_learning_disabled", "hydradb_graph")}
    tool_by_key = {tool.key: tool for tool in catalog}
    for i, order in enumerate(heldout):
        name, template, expected = CASES[i]
        task = template.format(id=order["order_id"])
        candidates = candidate_skills(task)
        route = await route_graph_candidates(db, candidates, order["order_id"])
        before = await route_graph_candidates(db, candidates, order["order_id"], exclude_dependencies=learned_edges) if learned_edges else route
        graph_keys = {r["key"] for r in route["tools"] if r["key"] in tool_by_key}
        before_keys = {r["key"] for r in before["tools"] if r["key"] in tool_by_key}
        flat = flat_candidates(task, candidates, expand=False)
        expanded = flat_candidates(task, candidates, expand=True)
        sem_pool = [tool for tool in catalog if tool.skill in {x["skill"] for x in candidates}]
        # Skill-semantic candidate set plus full transitive declared dependencies.
        from .router import expanded_flat_retrieve
        semantic = expanded_flat_retrieve(task, sem_pool, limit=max(1, len(sem_pool)))
        selected_by_method = {
            "full_context": catalog,
            "flat_lexical": flat,
            "flat_dependency_expanded": expanded,
            "semantic_dependency_expanded": semantic,
            "graph_learning_disabled": [tool_by_key[k] for k in before_keys],
            "hydradb_graph": [tool_by_key[k] for k in graph_keys],
        }
        outputs = {}
        for method, selected in selected_by_method.items():
            executed = execute_deterministic(task, order["order_id"], selected)
            keys = {tool.key for tool in selected}
            recall = len(expected & keys)/len(expected)
            outputs[method] = {"success": bool(expected.issubset({row["tool"] for row in executed["tool_outputs"] if not row.get("error")})),
                "tool_recall": round(recall, 3), "selected_tools": sorted(keys),
                "tool_calls": executed["tool_calls"], "dependency_failures": executed["dependency_failures"],
                "context_tokens_est": max(1, sum(len(json.dumps(tool_json(t), separators=(",", ":"))) for t in selected)//4)}
            method_totals[method].append(outputs[method])
        case_results.append({"name": name, "task": task, "order_id": order["order_id"], "candidate_skills": candidates,
            "expected_tools": sorted(expected), "resolved_entities": route["resolved_entities"], "methods": outputs,
            "graph_edges": route["edges"]})
    summaries = {}
    for method, rows in method_totals.items():
        summaries[method] = {"task_completion_rate": round(sum(r["success"] for r in rows)/len(rows), 3),
            "required_tool_recall": round(sum(r["tool_recall"] for r in rows)/len(rows), 3),
            "mean_context_tokens_est": round(sum(r["context_tokens_est"] for r in rows)/len(rows), 1),
            "mean_tool_calls": round(sum(r["tool_calls"] for r in rows)/len(rows), 2)}
    return {"suite": "olist_deterministic_heldout_v1", "real_model_calls": 0,
        "limitations": ["Authored nine-case suite; descriptive results only, not statistically significant.",
            "Tool execution is deterministic over source rows; this does not measure Groq reasoning quality.",
            "Olist has no official refund policy; refund behavior is explicitly simulated."],
        "training_order_ids_reserved": [row["order_id"] for row in orders[:2]],
        "methods": summaries, "cases": case_results}
