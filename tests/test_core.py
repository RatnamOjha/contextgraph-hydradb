import unittest

from backend.fixtures import TASKS, make_catalog
from backend.router import classify, execute_fixture, flat_retrieve


class ContextGraphCoreTests(unittest.TestCase):
    def test_intent_routes_known_support_tasks(self):
        self.assertEqual(classify("is this order eligible for a refund?"), "refund")
        self.assertEqual(classify("invoice and disputed payment charge"), "billing")
        self.assertEqual(classify("the package delivery is late"), "delivery")

    def test_flat_retrieval_uses_only_catalog_tools(self):
        catalog = make_catalog(100)
        result = flat_retrieve("check refund eligibility for order", catalog, limit=8)
        self.assertLessEqual(len(result), 8)
        self.assertTrue(any(tool.key == "refund.eligibility" for tool in result))
        self.assertTrue(all(tool in catalog for tool in result))

    def test_cached_entity_satisfies_resolution_dependencies(self):
        fixture = TASKS["refund"]
        resolved = [{"type": "ticket", "id": fixture["ticket"]}, {"type": "customer", "id": fixture["customer"]}, {"type": "order", "id": fixture["order"]}]
        available = set(fixture["required"]) - {"customer.resolve", "orders.list"}
        result = execute_fixture("refund", "graph", available, resolved)
        self.assertTrue(result["success"], result["missing_tools"])
        self.assertEqual(result["tool_calls"], 3)
        self.assertTrue(any(row["tool"] == "refund.eligibility" for row in result["tool_outputs"]))

    def test_missing_dependency_fails_safely(self):
        result = execute_fixture("refund", "graph", {"ticket.lookup", "refund.eligibility"}, [])
        self.assertFalse(result["success"])
        self.assertFalse(result["dependency_failures"] == [])
        self.assertEqual(result["answer"], "The available context is missing required tools, so the task could not be completed safely.")


if __name__ == "__main__":
    unittest.main()
