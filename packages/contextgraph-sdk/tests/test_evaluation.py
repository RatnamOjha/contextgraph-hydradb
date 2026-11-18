import asyncio
import json

import httpx
import pytest

from contextgraph import AsyncContextGraph, ContextGraph


TOOLS = [
    {"key": "commerce.check_refund", "name": "Check refund", "description": "Check refund eligibility",
     "schema": {"type": "object", "properties": {"order_id": {"type": "string"}}},
     "server": "commerce", "catalog": "commerce", "dependencies": ["commerce.get_order"]},
    {"key": "commerce.get_order", "name": "Get order", "description": "Read order details",
     "schema": {"type": "object", "properties": {"order_id": {"type": "string"}}},
     "server": "commerce", "catalog": "commerce", "dependencies": []},
    {"key": "commerce.get_payment", "name": "Get payment", "description": "Check refund status for payment records",
     "schema": {"type": "object", "properties": {"order_id": {"type": "string"},
        "payment_id": {"type": "string"}, "currency": {"type": "string"},
        "amount": {"type": "number"}, "method": {"type": "string"}, "status": {"type": "string"}}},
     "server": "commerce", "catalog": "commerce", "dependencies": []},
]


def graph_result(task):
    return {"task": task, "catalog": "commerce", "skills": [], "resolved_entities": [],
        "retrieval_ms": 2.0, "bounded": {"max_tools": 3}, "complete": True,
        "missing_dependencies": [], "omitted_tool_ids": [],
        "nodes": [
            {"id": "commerce.check_refund", "label": "Check refund", "type": "Tool", "metadata": {
                "name": "Check refund", "description": "Check refund eligibility", "schema": TOOLS[0]["schema"],
                "server": "commerce", "dependencies": ["commerce.get_order"], "why": ["task match"],
                "execution_provenance": "developer_registered"}},
            {"id": "commerce.get_order", "label": "Get order", "type": "Tool", "metadata": {
                "name": "Get order", "description": "Read order details", "schema": TOOLS[1]["schema"],
                "server": "commerce", "dependencies": [], "why": ["required prerequisite"],
                "execution_provenance": "developer_registered"}},
        ],
        "edges": [{"from": "commerce.check_refund", "to": "commerce.get_order", "type": "DEPENDS_ON"}],
    }


def make_handler():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"tools": TOOLS})
        payload = json.loads(request.content)
        return httpx.Response(200, json=graph_result(payload["task"]))
    return handler


TASKS = [{"task": "Evaluate refund eligibility for the request", "required_tools": ["commerce.check_refund"]}]


def test_sync_evaluator_reports_graph_and_flat_retrieval_metrics():
    client = httpx.Client(transport=httpx.MockTransport(make_handler()))
    with ContextGraph("https://example.test", retries=0, http_client=client) as graph:
        report = graph.evaluate("commerce", TASKS, max_tools=3)
    row = report.tasks[0]
    assert row.graph.required_tool_recall == 1.0
    assert row.flat.required_tool_recall == 1.0
    assert row.graph.dependency_complete is True
    assert row.flat.dependency_complete is False
    assert row.flat.unrelated_tool_count == 1
    assert row.graph.unrelated_tool_count == 0
    assert row.graph.estimated_schema_tokens < row.flat.estimated_schema_tokens
    assert report.graph_average_required_tool_recall == 1.0
    assert report.graph_dependency_complete_rate == 1.0
    assert report.flat_dependency_complete_rate == 0.0
    assert "does not measure LLM accuracy" in report.measurement_note


def test_async_evaluator_has_same_labels_and_no_model_calls():
    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(make_handler()))
        async with AsyncContextGraph("https://example.test", retries=0, http_client=client) as graph:
            return await graph.evaluate("commerce", TASKS, max_tools=3)
    report = asyncio.run(run())
    assert report.tasks[0].graph.selected_tool_ids == ["commerce.check_refund", "commerce.get_order"]
    assert report.tasks[0].graph.dependency_complete is True
    assert report.tasks[0].flat.dependency_complete is False


def test_evaluator_rejects_unknown_labels_and_bad_task_specs_before_retrieval():
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"tools": TOOLS})))
    with ContextGraph("https://example.test", retries=0, http_client=client) as graph:
        with pytest.raises(ValueError, match="not in catalog"):
            graph.evaluate("commerce", [{"task": "Check a unique thing", "required_tools": ["commerce.unknown"]}])
        with pytest.raises(ValueError, match="required_tools"):
            graph.evaluate("commerce", [{"task": "Check a unique thing", "required_tools": []}])
