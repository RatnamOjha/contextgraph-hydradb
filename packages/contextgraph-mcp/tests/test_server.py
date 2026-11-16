from __future__ import annotations

import sys
import unittest
from unittest.mock import patch

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from contextgraph_mcp import server


GRAPH_RESPONSE = {
    "task": "Which product categories were purchased in this order?",
    "order_id": "35a972d7f8436f405b56e36add1a7140",
    "skills": [{"skill": "product_research", "score": 1, "evidence": ["categories"]}],
    "nodes": [
        {"id": "order.items", "type": "Tool", "metadata": {
            "description": "Read source line items", "input_schema": "{order_id: string}",
            "server": "olist-data", "dependencies": ["order.details"],
            "execution_provenance": "dataset_backed", "why": ["Skill product_research matched task terms"]}},
        {"id": "order.details", "type": "Tool", "metadata": {
            "description": "Read order details", "input_schema": "{order_id: string}",
            "server": "olist-data", "dependencies": [], "execution_provenance": "dataset_backed"}},
    ],
    "edges": [
        {"from": "product_research", "to": "order.items", "type": "USES"},
        {"from": "order.items", "to": "order.details", "type": "DEPENDS_ON"},
    ],
    "resolved_entities": [{"type": "order", "id": "35a972d7f8436f405b56e36add1a7140"}],
    "retrieval_ms": 1.4,
}

TOOLS_RESPONSE = {"tools": [{
    "key": "order.items", "name": "Read order items", "description": "Read source line items",
    "schema": "{order_id: string}", "server": "olist-data", "skill": "product_research",
    "execution_status": "dataset_backed", "source": "Olist historical dataset",
    "dependencies": ["order.details"],
}, {
    "key": "order.details", "name": "Read order details", "description": "Read order details",
    "schema": "{order_id: string}", "server": "olist-data", "skill": "order_support",
    "execution_status": "dataset_backed", "source": "Olist historical dataset", "dependencies": [],
}]}


class ContextGraphMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_tools_return_actual_route_and_inspection_metadata(self):
        async def fake_request(path: str, params=None):
            return GRAPH_RESPONSE if path == "/api/graph/explore" else TOOLS_RESPONSE

        with patch.object(server, "request_json", new=fake_request):
            async with Client(server.mcp) as client:
                listed = await client.list_tools()
                self.assertEqual({tool.name for tool in listed.tools}, {"retrieve_context", "inspect_tool", "explain_context"})
                retrieved = await client.call_tool("retrieve_context", {
                    "task": GRAPH_RESPONSE["task"], "order_id": GRAPH_RESPONSE["order_id"]})
                self.assertFalse(retrieved.is_error)
                data = retrieved.structured_content
                self.assertEqual([tool["id"] for tool in data["selected_tools"]], ["order.items", "order.details"])
                self.assertEqual(data["required_dependencies"], [GRAPH_RESPONSE["edges"][1]])
                self.assertFalse(data["execution_performed"])
                inspected = await client.call_tool("inspect_tool", {"tool_id": "order.items"})
                self.assertEqual(inspected.structured_content["prerequisites"], ["order.details"])
                explained = await client.call_tool("explain_context", {"task": GRAPH_RESPONSE["task"]})
                self.assertEqual(explained.structured_content["dependency_expansions"], [GRAPH_RESPONSE["edges"][1]])

    async def test_invalid_order_reference_is_rejected(self):
        async def must_not_call(*args, **kwargs):
            raise AssertionError("invalid order ID must be rejected before HTTP request")

        with patch.object(server, "request_json", new=must_not_call):
            with self.assertRaises(ValueError):
                await server.retrieve_context("A valid task length", "not-an-order-id")

    async def test_stdio_protocol_lists_tools(self):
        async with Client(StdioServerParameters(command=sys.executable,
                args=["-m", "contextgraph_mcp.server"])) as client:
            listed = await client.list_tools()
            self.assertEqual({tool.name for tool in listed.tools}, {"retrieve_context", "inspect_tool", "explain_context"})


if __name__ == "__main__":
    unittest.main()
