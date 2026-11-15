import unittest

from backend.objectives import evaluate_answer, requested_objectives


class ObjectiveEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.task = "Summarize this order status, payment total, and delivery timing."
        self.outputs = [
            {"tool": "order.details", "arguments": {}, "result": {"data": {"status": "shipped", "total": 92.25, "purchase_at": "2018-09-01 12:00:00"}}},
            {"tool": "order.payments", "arguments": {}, "result": {"data": [{"kind": "credit_card", "installments": 2, "value": 100.0}]}},
            {"tool": "order.delivery_status", "arguments": {}, "result": {"data": {"source_status": "shipped", "delivered_at": None, "estimated_at": "2018-09-10 00:00:00", "delivery_outcome": "not_delivered_in_source"}}},
        ]

    def test_multi_intent_partial_when_delivery_is_unverifiable(self):
        self.assertEqual([o["id"] for o in requested_objectives(self.task)], ["status", "payment_total", "delivery_timing"])
        result = evaluate_answer(self.task, "Status: shipped. Payment total: BRL 100,00. Delivery timing is unverified.", self.outputs)
        self.assertEqual(result["code"], "PARTIAL")
        self.assertEqual([o["factually_correct"] for o in result["objectives"]], [True, True, None])
        self.assertFalse(result["evidence"]["delivery_timing"]["sufficient"])

    def test_item_total_cannot_prove_payment_total(self):
        result = evaluate_answer(self.task, "Status: shipped. Payment total: BRL 92.25. Delivery was not established.", self.outputs)
        payment = next(row for row in result["objectives"] if row["id"] == "payment_total")
        self.assertFalse(payment["factually_correct"])
        self.assertTrue(result["unsupported_claims"])

    def test_answer_without_payment_evidence_does_not_pass(self):
        result = evaluate_answer(self.task, "The order status is shipped. The payment total is BRL 100.00; delivery is unverified.", [self.outputs[0]])
        self.assertNotEqual(result["code"], "SUCCESS")
        self.assertFalse(next(row for row in result["objectives"] if row["id"] == "payment_total")["evidence_sufficient"])

    def test_missing_delivery_time_is_not_non_delivery(self):
        result = evaluate_answer(self.task, "Status: shipped. Payment total BRL 100.00. This was never delivered.", self.outputs)
        self.assertTrue(any(row["type"] == "delivery_claim" for row in result["unsupported_claims"]))

    def test_missing_tool_prerequisite_is_blocked(self):
        outputs = [{"tool": "order.items", "error": True,
                    "result": {"error": "Required tool dependencies were not executed first: order.details"}}]
        result = evaluate_answer("List line items for order", "I could not retrieve the line items.", outputs)
        self.assertEqual(result["code"], "BLOCKED")


if __name__ == "__main__": unittest.main()
