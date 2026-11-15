"""One bounded live Groq test; skipped unless a server-side key is configured."""
import os
import unittest
from backend.config import load_env_file

load_env_file()

from backend.dataset_workflow import TOOL_BY_KEY
from backend.fixtures import DATA_TOOLS
from backend.groq_agent import run_groq_agent
from backend.hydra import HydraClient
from backend.olist_store import available, list_orders
from backend.router import candidate_skills, route_graph_candidates


@unittest.skipUnless(os.getenv("GROQ_API_KEY"), "GROQ_API_KEY is not configured")
class GroqAgentIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_graph_selected_tool_runs_on_olist_and_grounded_answer_returns(self):
        if not available(): self.skipTest("Olist SQLite subset is not installed")
        orders = list_orders(1)
        order_id = orders[0]["order_id"]
        task = f"Read the historical status and total for order {order_id}."
        db = HydraClient()
        await db.open()
        try:
            candidates = candidate_skills(task)
            route = await route_graph_candidates(db, candidates, order_id)
            keys = {row["key"] for row in route["tools"]}
            self.assertIn("order.details", keys, "The tool must come from HydraDB skill/dependency traversal")
            selected = [tool for tool in DATA_TOOLS if tool.key in keys]
            self.assertTrue(selected)
            result = await run_groq_agent(task, selected, required_order_id=order_id)
            self.assertTrue(any("received" in row["message"] for row in result["trace"]))
            self.assertGreaterEqual(result["agent"]["provider_calls"], 2, "One call selects a tool; a later call sees the tool result")
            self.assertGreaterEqual(result["tool_calls"], 1)
            called = next(row for row in result["tool_outputs"] if row["tool"] == "order.details")
            self.assertEqual(called["arguments"], {"order_id": order_id})
            self.assertEqual(called["result"]["source"], "Olist Brazilian E-Commerce Public Dataset")
            self.assertTrue(result["answer"].strip())
            self.assertTrue(result["success"], result["trace"])
        finally:
            await db.close()


if __name__ == "__main__": unittest.main()
