import csv
import tempfile
import unittest
import zipfile
from pathlib import Path

from backend.olist_store import execute_data_tool, import_olist, list_orders, lookup_order, order_items, order_payments


class OlistStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.archive = self.root / "olist.zip"
        self.database = self.root / "demo.sqlite"
        self.ids = ["a" * 32, "b" * 32]
        tables = {
            "olist_orders_dataset.csv": [
                {"order_id": self.ids[0], "customer_id": "c"*32, "order_status": "delivered", "order_purchase_timestamp": "2018-10-16 10:00:00", "order_approved_at": "2018-10-16 10:01:00", "order_delivered_carrier_date": "2018-10-16 12:00:00", "order_delivered_customer_date": "2018-10-17 12:00:00", "order_estimated_delivery_date": "2018-10-18 00:00:00"},
                {"order_id": self.ids[1], "customer_id": "d"*32, "order_status": "delivered", "order_purchase_timestamp": "2018-10-15 10:00:00", "order_approved_at": "2018-10-15 10:01:00", "order_delivered_carrier_date": "2018-10-15 12:00:00", "order_delivered_customer_date": "2018-10-17 12:00:00", "order_estimated_delivery_date": "2018-10-18 00:00:00"},
            ],
            "olist_customers_dataset.csv": [
                {"customer_id":"c"*32,"customer_unique_id":"e"*32}, {"customer_id":"d"*32,"customer_unique_id":"f"*32}],
            "olist_order_items_dataset.csv": [
                {"order_id":self.ids[0],"order_item_id":"1","product_id":"1"*32,"seller_id":"2"*32,"price":"10.00","freight_value":"2.00"},
                {"order_id":self.ids[1],"order_item_id":"1","product_id":"3"*32,"seller_id":"4"*32,"price":"50.00","freight_value":"5.00"}],
            "olist_order_payments_dataset.csv": [
                {"order_id":self.ids[0],"payment_sequential":"1","payment_type":"credit_card","payment_installments":"1","payment_value":"12.00"},
                {"order_id":self.ids[1],"payment_sequential":"1","payment_type":"boleto","payment_installments":"2","payment_value":"55.00"}],
            "olist_products_dataset.csv": [
                {"product_id":"1"*32,"product_category_name":"books","product_weight_g":"200"},
                {"product_id":"3"*32,"product_category_name":"toys","product_weight_g":"500"}],
            "olist_sellers_dataset.csv": [
                {"seller_id":"2"*32,"seller_state":"SP"}, {"seller_id":"4"*32,"seller_state":"RJ"}],
        }
        with zipfile.ZipFile(self.archive, "w") as zf:
            for name, rows in tables.items():
                fields = list(rows[0])
                from io import StringIO
                output = StringIO()
                writer = csv.DictWriter(output, fieldnames=fields)
                writer.writeheader(); writer.writerows(rows)
                zf.writestr(name, output.getvalue())

    def tearDown(self): self.tmp.cleanup()

    def test_deterministic_import_keeps_real_relationships_and_distinct_orders(self):
        first = import_olist(self.archive, self.database, 2)
        self.assertEqual(first["orders"], 2)
        newest = list_orders(2, self.database)
        self.assertEqual({r["order_id"] for r in newest}, set(self.ids))
        self.assertEqual(lookup_order(self.ids[0], self.database)["total"], 12.0)
        self.assertEqual(lookup_order(self.ids[1], self.database)["total"], 55.0)
        self.assertEqual(order_items(self.ids[0], self.database)[0]["category"], "books")
        self.assertEqual(order_payments(self.ids[1], self.database)[0]["kind"], "boleto")

    def test_unknown_ids_and_invalid_arguments_fail_safely(self):
        import_olist(self.archive, self.database, 2)
        self.assertIsNone(lookup_order("f"*32, self.database))
        self.assertIn("error", execute_data_tool("order.details", {"order_id":"f"*32}, self.database))
        self.assertIn("error", execute_data_tool("order.details", {"order_id":self.ids[0], "extra":1}, self.database))


if __name__ == "__main__": unittest.main()
