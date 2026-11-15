import unittest

from backend.dataset_workflow import _grounded_answer
from backend.objectives import evaluate_answer


def output(tool, data):
    return {"tool": tool, "arguments": {}, "result": {"data": data}, "error": False}


class FixtureAnswerTests(unittest.TestCase):
    def test_payment_methods_values_and_installments_are_explicit_and_verified(self):
        task = "Compare payment methods and installment amounts for this order"
        outputs = [output("order.payments", [
            {"sequence": 1, "kind": "credit_card", "value": 42.50, "installments": 1},
        ])]
        answer = _grounded_answer(task, "order-1", outputs)
        self.assertIn("Only one payment method is present: credit card", answer)
        self.assertIn("1 payment record", answer)
        self.assertIn("payment value BRL 42.50", answer)
        self.assertIn("1 installment", answer)
        self.assertNotIn("compared", answer.lower())
        result = evaluate_answer(task, answer, outputs, fixture_mode=True)
        self.assertEqual(result["code"], "SUCCESS")
        self.assertTrue(all(row["factually_correct"] is True for row in result["objectives"]))

    def test_multiple_payments_with_one_method_are_not_misrepresented_as_multiple_methods(self):
        task = "Compare payment methods and installment amounts for this order"
        outputs = [output("order.payments", [
            {"sequence": 1, "kind": "credit_card", "value": 20, "installments": 2},
            {"sequence": 2, "kind": "credit_card", "value": 30, "installments": 3},
        ])]
        answer = _grounded_answer(task, "order-2", outputs)
        self.assertIn("There are 2 payment records", answer)
        self.assertIn("Only one payment method is present: credit card", answer)
        self.assertIn("2 installments", answer)
        self.assertIn("3 installments", answer)
        self.assertEqual(evaluate_answer(task, answer, outputs, fixture_mode=True)["code"], "SUCCESS")

    def test_multiple_payment_methods_are_named_and_counted(self):
        task = "Compare payment methods for this order"
        outputs = [output("order.payments", [
            {"sequence": 1, "kind": "credit_card", "value": 20, "installments": 1},
            {"sequence": 2, "kind": "boleto", "value": 30, "installments": 1},
        ])]
        answer = _grounded_answer(task, "order-3", outputs)
        self.assertIn("There are 2 payment records", answer)
        self.assertIn("Multiple payment methods are present: boleto, credit card", answer)
        self.assertEqual(evaluate_answer(task, answer, outputs, fixture_mode=True)["code"], "SUCCESS")

    def test_line_items_and_empty_item_list_are_verified_from_source(self):
        task = "List the line items for this order"
        data = [{"product_id": "p-1", "category": "books"}, {"product_id": "p-2", "category": "toys"}]
        outputs = [output("order.items", data)]
        answer = _grounded_answer(task, "order-4", outputs)
        self.assertIn("2 line items", answer)
        self.assertIn("books, toys", answer)
        self.assertEqual(evaluate_answer(task, answer, outputs, fixture_mode=True)["code"], "SUCCESS")

        empty_outputs = [output("order.items", [])]
        empty_answer = _grounded_answer(task, "order-5", empty_outputs)
        self.assertIn("0 line items", empty_answer)
        self.assertEqual(evaluate_answer(task, empty_answer, empty_outputs, fixture_mode=True)["code"], "SUCCESS")

    def test_order_status_is_stated_and_verified(self):
        task = "What is the order status?"
        outputs = [output("order.details", {"status": "shipped"})]
        answer = _grounded_answer(task, "order-6", outputs)
        self.assertIn("Order status: shipped", answer)
        self.assertEqual(evaluate_answer(task, answer, outputs, fixture_mode=True)["code"], "SUCCESS")

    def test_delivery_timestamps_and_missing_timestamp_remain_distinct(self):
        task = "Check delivery timing and timestamps for this order"
        outputs = [output("order.delivery_status", {
            "delivered_at": "2018-09-12 10:00:00", "estimated_at": "2018-09-10 00:00:00",
            "delivery_outcome": "delivered_late",
        })]
        answer = _grounded_answer(task, "order-7", outputs)
        self.assertIn("actual 2018-09-12", answer)
        self.assertIn("estimated 2018-09-10", answer)
        self.assertIn("delivered late", answer)
        self.assertEqual(evaluate_answer(task, answer, outputs, fixture_mode=True)["code"], "SUCCESS")

        missing = [output("order.delivery_status", {
            "delivered_at": None, "estimated_at": "2018-09-10 00:00:00",
            "delivery_outcome": "not_delivered_in_source",
        })]
        missing_answer = _grounded_answer(task, "order-8", missing)
        self.assertIn("actual timestamp is missing", missing_answer)
        self.assertNotIn("not delivered", missing_answer.lower())
        self.assertEqual(evaluate_answer(task, missing_answer, missing, fixture_mode=True)["code"], "UNVERIFIED")

    def test_missing_payment_value_is_not_filled_or_verified(self):
        task = "What is the payment total for this order?"
        outputs = [output("order.payments", [
            {"sequence": 1, "kind": "credit_card", "value": None, "installments": 1},
        ])]
        answer = _grounded_answer(task, "order-9", outputs)
        self.assertIn("payment values are missing", answer)
        result = evaluate_answer(task, answer, outputs, fixture_mode=True)
        self.assertEqual(result["code"], "UNVERIFIED")
        self.assertFalse(result["evidence"]["payment_total"]["sufficient"])

        multi_task = "Compare payment methods and installment amounts for this order"
        multi_outputs = [output("order.payments", [
            {"sequence": 1, "kind": "credit_card", "value": None, "installments": 1},
        ])]
        multi_answer = _grounded_answer(multi_task, "order-9", multi_outputs)
        multi_result = evaluate_answer(multi_task, multi_answer, multi_outputs, fixture_mode=True)
        self.assertEqual(multi_result["code"], "PARTIAL")
        self.assertEqual(multi_result["verified_objectives"], 2)

    def test_multi_objective_answer_verifies_each_available_fact(self):
        task = "Summarize order status, payment methods, payment total, delivery timing, and line items"
        outputs = [
            output("order.details", {"status": "delivered", "total": 42.5}),
            output("order.payments", [{"sequence": 1, "kind": "boleto", "value": 42.5, "installments": 1}]),
            output("order.delivery_status", {"delivered_at": "2018-09-12 10:00:00", "estimated_at": "2018-09-10 00:00:00", "delivery_outcome": "delivered_late"}),
            output("order.items", [{"product_id": "p-9", "category": "books"}]),
        ]
        answer = _grounded_answer(task, "order-10", outputs)
        result = evaluate_answer(task, answer, outputs, fixture_mode=True)
        self.assertEqual(result["code"], "SUCCESS")
        self.assertEqual(result["verified_objectives"], result["total_objectives"])


if __name__ == "__main__":
    unittest.main()
