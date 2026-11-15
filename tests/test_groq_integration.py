"""One bounded live Groq test; skipped unless a server-side key is configured."""
import os
import unittest
from backend.config import load_env_file

load_env_file()

from backend.dataset_workflow import TOOL_BY_KEY
from backend.fixtures import DATA_TOOLS
from backend.groq_agent import run_groq_agent
from backend.hydra import HydraClient
from backend.olist_store import available, list_orders, lookup_order
from backend.router import candidate_skills, route_graph_candidates


@unittest.skipUnless(os.getenv("GROQ_API_KEY"), "GROQ_API_KEY is not configured")
class GroqAgentIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_graph_selected_tool_runs_on_olist_and_grounded_answer_returns(self):
        if not available(): self.skipTest("Olist SQLite subset is not installed")
        orders = list_orders(200)
        source_order = next((row for row in orders if (details := lookup_order(row["order_id"]))
                            and details.get("delivered_at") and details.get("estimated_at")), None)
        if not source_order: self.skipTest("Imported subset contains no row with both delivery timestamps")
        order_id = source_order["order_id"]
        task = f"Summarize this order's status, payment total, and delivery timing. Order ID: {order_id}."
        db = HydraClient()
        await db.open()
        try:
            try:
                await db.query(f"MATCH (n:Agent {{id: {db.id_for('Agent', 'support-agent')}}}) RETURN n.key AS key")
            except Exception as exc:
                self.skipTest(f"Local HydraDB is not running: {type(exc).__name__}")
            candidates = candidate_skills(task)
            route = await route_graph_candidates(db, candidates, order_id)
            keys = {row["key"] for row in route["tools"]}
            self.assertIn("order.details", keys, "The tool must come from HydraDB skill/dependency traversal")
            selected = [tool for tool in DATA_TOOLS if tool.key in keys]
            self.assertTrue(selected)
            result = await run_groq_agent(task, selected, required_order_id=order_id)
            self.assertTrue(any("received" in row["message"] for row in result["trace"]))
            self.assertGreaterEqual(result["agent"]["provider_calls"], 3, "Multi-intent task should gather evidence across provider iterations")
            self.assertGreaterEqual(result["tool_calls"], 2)
            called = {row["tool"]: row for row in result["tool_outputs"]}
            self.assertIn("order.details", called)
            self.assertIn("order.payments", called)
            self.assertIn("order.delivery_status", called)
            self.assertEqual(called["order.details"]["arguments"], {"order_id": order_id})
            self.assertEqual(called["order.details"]["result"]["source"], "Olist Brazilian E-Commerce Public Dataset")
            self.assertTrue(result["answer"].strip())
            self.assertEqual(result["outcome"]["code"], "SUCCESS", result["outcome"])
        finally:
            await db.close()


if __name__ == "__main__": unittest.main()
