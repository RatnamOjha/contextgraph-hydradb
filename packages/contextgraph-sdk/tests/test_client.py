import asyncio
import httpx
import pytest

from contextgraph import AsyncContextGraph, ContextGraph, ContextGraphError


GRAPH = {
    "task": "What is the order status?",
    "order_id": None,
    "skills": [{"skill": "order_support", "score": 1.0, "evidence": ["order"]}],
    "nodes": [
        {"id": "order.details", "label": "Order details", "type": "Tool", "metadata": {
            "name": "Order details", "description": "Read an order", "input_schema": '{"type":"object"}',
            "server": "olist-data", "dependencies": [], "why": ["Matched order status"],
            "execution_provenance": "dataset_backed"}},
        {"id": "order:abc", "label": "order:abc", "type": "Entity", "metadata": {"entity_type": "order"}},
    ],
    "edges": [], "resolved_entities": [{"type": "order", "id": "abc"}],
    "retrieval_ms": 2.4, "bounded": {"max_nodes": 150},
}


def mock_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_sync_client_normalizes_catalog_and_graph_and_never_executes():
    def handler(request):
        assert request.method == "GET"
        if request.url.path == "/api/health":
            return httpx.Response(200, json={"ok": True, "hydradb": "connected", "probe_ms": 1.2})
        if request.url.path == "/api/tools":
            return httpx.Response(200, json={"tools": [{"key": "order.details", "name": "Order details",
                "schema": '{"type":"object"}', "source": "Olist historical dataset", "dependencies": []}]})
        assert request.url.path == "/api/graph/explore"
        assert request.url.params["task"] == GRAPH["task"]
        return httpx.Response(200, json=GRAPH)

    with ContextGraph("https://example.test", retries=0, http_client=mock_client(handler)) as cg:
        assert cg.health().hydradb == "connected"
        tool = cg.inspect_tool("order.details")
        assert tool.input_schema == {"type": "object"}
        result = cg.retrieve(GRAPH["task"])
        assert [tool.id for tool in result.tools] == ["order.details"]
        assert result.selection_reasons["order.details"] == ["Matched order status"]
        assert result.execution_performed is False
        explanation = cg.explain(GRAPH["task"])
        assert explanation.selected_tool_ids == ["order.details"]


def test_async_client_uses_same_normalization():
    async def run():
        async def handler(request):
            return httpx.Response(200, json=GRAPH)
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async with AsyncContextGraph("https://example.test", retries=0, http_client=client) as cg:
            result = await cg.retrieve(GRAPH["task"])
            assert result.tools[0].id == "order.details"
            assert result.execution_performed is False
    asyncio.run(run())


def test_transport_error_is_typed_and_does_not_echo_credentials():
    secret = "never-print-this-key"
    def handler(request):
        return httpx.Response(503, json={"detail": "temporarily unavailable"})
    client = mock_client(handler)
    with ContextGraph("https://example.test", api_key=secret, retries=0, http_client=client) as cg:
        with pytest.raises(ContextGraphError) as caught:
            cg.health()
    assert caught.value.status_code == 503
    assert caught.value.retryable is True
    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)


def test_invalid_task_is_rejected_before_network_call():
    client = mock_client(lambda request: pytest.fail("unexpected HTTP request"))
    with ContextGraph("https://example.test", retries=0, http_client=client) as cg:
        with pytest.raises(ValueError, match="8–1200"):
            cg.retrieve("short")


def test_retry_only_repeats_idempotent_get():
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={"detail": "retry"})
        return httpx.Response(200, json={"ok": True, "hydradb": "connected"})
    with ContextGraph("https://example.test", retries=1, http_client=mock_client(handler)) as cg:
        assert cg.health().ok is True
    assert calls == 2

