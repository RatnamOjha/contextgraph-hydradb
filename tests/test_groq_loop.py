import json
import unittest
from unittest.mock import patch

from backend.fixtures import DATA_TOOLS
from backend.groq_agent import run_groq_agent
from backend.olist_store import execute_data_tool, list_orders, lookup_order, order_payments, delivery_status


class FakeResponse:
    status_code = 200
    headers = {}
    is_error = False
    def __init__(self, message): self.message = message
    def json(self): return {"choices": [{"message": self.message}], "usage": {"prompt_tokens": 10, "completion_tokens": 3}}


class FakeClient:
    sequence = []
    def __init__(self, *args, **kwargs): self.calls = 0
    async def post(self, *args, **kwargs):
        message = self.sequence[self.calls]
        self.calls += 1
        return FakeResponse(message)
    async def aclose(self): pass


class GroqLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_requests_missing_evidence_then_verifies_all_objectives(self):
        order = next(row for row in list_orders(200) if (d := lookup_order(row["order_id"])) and d["delivered_at"] and d["estimated_at"])
        order_id = order["order_id"]
        task = f"Summarize this order's status, payment total, and delivery timing. Order ID: {order_id}."
        data = {
            "order.details": execute_data_tool("order.details", {"order_id": order_id}),
            "order.payments": execute_data_tool("order.payments", {"order_id": order_id}),
            "order.delivery_status": execute_data_tool("order.delivery_status", {"order_id": order_id}),
        }
        calls = lambda key, ix: {"id": f"call_{ix}", "type": "function", "function": {"name": key.replace(".", "__"), "arguments": json.dumps({"order_id": order_id})}}
        payments = order_payments(order_id)
        delivery = delivery_status(order_id)
        timing = "delivered late" if delivery["delivery_outcome"] == "delivered_late" else "delivered on time"
        answer = f"Order status: {lookup_order(order_id)['status']}. Payment total: BRL {sum(r['value'] for r in payments):.2f}. It was {timing}; actual {delivery['delivered_at'][:10]}, estimated {delivery['estimated_at'][:10]}."
        FakeClient.sequence = [
            {"tool_calls": [calls("order.details", 1)]},
            {"content": "The order is shipped."},
            {"tool_calls": [calls("order.payments", 2)]},
            {"tool_calls": [calls("order.delivery_status", 3)]},
            {"content": answer},
        ]
        selected = [tool for tool in DATA_TOOLS if tool.key in data]
        with patch("backend.groq_agent.httpx.AsyncClient", FakeClient), patch.dict("os.environ", {"GROQ_API_KEY": "test-only"}):
            result = await run_groq_agent(task, selected, required_order_id=order_id, provider_request_budget=5)
        self.assertEqual(result["outcome"]["code"], "SUCCESS")
        self.assertEqual(result["agent"]["provider_calls"], 5)
        self.assertEqual({row["tool"] for row in result["tool_outputs"]}, set(data))
        self.assertEqual(result["execution"]["tools_exposed"], sorted(data))
        self.assertEqual(result["execution"]["tools_called"], sorted(data))

    async def test_dependency_rejection_can_retry_after_retrieved_prerequisite(self):
        order_id = list_orders(1)[0]["order_id"]
        task = f"List line items and categories for reference {order_id}."
        def call(key, n): return {"id": f"call_{n}", "type": "function", "function": {"name": key.replace('.', '__'), "arguments": json.dumps({"order_id": order_id})}}
        items = execute_data_tool("order.items", {"order_id": order_id})["data"]
        categories = sorted({row["category"] for row in items if row.get("category")})
        answer = "Line items: " + ", ".join(categories)
        FakeClient.sequence = [
            {"tool_calls": [call("order.items",1)]},
            {"tool_calls": [call("order.details",2)]},
            {"tool_calls": [call("order.items",3)]},
            {"content": answer},
        ]
        selected=[t for t in DATA_TOOLS if t.key in {"order.details","order.items"}]
        with patch("backend.groq_agent.httpx.AsyncClient",FakeClient),patch.dict("os.environ",{"GROQ_API_KEY":"test-only"}):
            result=await run_groq_agent(task,selected,required_order_id=order_id,provider_request_budget=4)
        self.assertEqual(result["outcome"]["code"],"SUCCESS",result["outcome"])
        self.assertEqual(result["execution"]["repeated_calls"],0)
        self.assertEqual([r["tool"] for r in result["tool_outputs"]], ["order.items","order.details","order.items"])
        self.assertTrue(result["tool_outputs"][0]["error"])
        self.assertFalse(result["tool_outputs"][2]["error"])


if __name__ == "__main__": unittest.main()
