"""Minimal, privacy-conscious Olist subset in SQLite; HydraDB stores context links."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterable
import zipfile

DEFAULT_DB = Path(os.getenv("CONTEXTGRAPH_DATA_DB", "data/olist_demo.sqlite"))
SOURCE_URL = "https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce"

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS customers (
  customer_id TEXT PRIMARY KEY, customer_unique_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS customers_unique_idx ON customers(customer_unique_id);
CREATE TABLE IF NOT EXISTS orders (
  order_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, status TEXT NOT NULL,
  purchase_at TEXT, approved_at TEXT, carrier_at TEXT, delivered_at TEXT, estimated_at TEXT
);
CREATE INDEX IF NOT EXISTS orders_customer_idx ON orders(customer_id);
CREATE TABLE IF NOT EXISTS products (
  product_id TEXT PRIMARY KEY, category TEXT, weight_g REAL
);
CREATE TABLE IF NOT EXISTS sellers (seller_id TEXT PRIMARY KEY, state TEXT);
CREATE TABLE IF NOT EXISTS order_items (
  order_id TEXT NOT NULL, item_no INTEGER NOT NULL, product_id TEXT NOT NULL,
  seller_id TEXT NOT NULL, price REAL NOT NULL, freight REAL NOT NULL,
  PRIMARY KEY(order_id, item_no)
);
CREATE INDEX IF NOT EXISTS items_product_idx ON order_items(product_id);
CREATE INDEX IF NOT EXISTS items_seller_idx ON order_items(seller_id);
CREATE TABLE IF NOT EXISTS payments (
  order_id TEXT NOT NULL, sequence INTEGER NOT NULL, kind TEXT NOT NULL,
  installments INTEGER NOT NULL, value REAL NOT NULL, PRIMARY KEY(order_id, sequence)
);
CREATE TABLE IF NOT EXISTS dataset_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    target = Path(path or DEFAULT_DB)
    if not target.exists():
        raise FileNotFoundError(f"Olist data is not imported. Expected SQLite database at {target}")
    conn = sqlite3.connect(f"file:{target.resolve()}?mode=ro", uri=True, timeout=3)
    conn.row_factory = sqlite3.Row
    return conn


def available(path: str | Path | None = None) -> bool:
    try:
        with connect(path) as conn:
            return conn.execute("SELECT 1 FROM orders LIMIT 1").fetchone() is not None
    except (OSError, sqlite3.Error):
        return False


def dataset_status(path: str | Path | None = None) -> dict[str, Any]:
    try:
        with connect(path) as conn:
            meta = {row["key"]: row["value"] for row in conn.execute("SELECT key,value FROM dataset_meta")}
            counts = {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("orders", "customers", "order_items", "payments", "products", "sellers")}
        return {"available": counts["orders"] > 0, "source": SOURCE_URL, "license": "CC BY-NC-SA 4.0", "counts": counts, "metadata": meta}
    except (OSError, sqlite3.Error):
        return {"available": False, "source": SOURCE_URL, "license": "CC BY-NC-SA 4.0", "counts": {}}


def _read_csv(zf: zipfile.ZipFile, suffix: str) -> Iterable[dict[str, str]]:
    name = next((n for n in zf.namelist() if n.endswith(suffix)), None)
    if name is None:
        if suffix.endswith("translation.csv"):
            return []
        raise ValueError(f"Olist archive is missing {suffix}")
    f = zf.open(name)
    import io
    return csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig", newline=""))


def import_olist(archive: str | Path, output: str | Path, order_limit: int = 500) -> dict[str, int | str]:
    """Import a stable recent-order subset. No source CSV or PII is copied out."""
    archive, output = Path(archive), Path(output)
    if order_limit < 1 or order_limit > 10000:
        raise ValueError("order_limit must be between 1 and 10000")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        order_rows = list(_read_csv(zf, "olist_orders_dataset.csv"))
        all_items = list(_read_csv(zf, "olist_order_items_dataset.csv"))
        item_order_ids = {r["order_id"] for r in all_items}
        order_rows.sort(key=lambda r: (r.get("order_purchase_timestamp", ""), r.get("order_id", "")), reverse=True)
        # Every demo order has source item/product/seller records; canceled or
        # otherwise incomplete orders remain eligible if the source has items.
        chosen = [r for r in order_rows if r["order_id"] in item_order_ids][:order_limit]
        order_ids = {r["order_id"] for r in chosen}
        customers = {r["customer_id"]: r for r in _read_csv(zf, "olist_customers_dataset.csv") if r["customer_id"] in {o["customer_id"] for o in chosen}}
        raw_items = [r for r in all_items if r["order_id"] in order_ids]
        product_ids = {r["product_id"] for r in raw_items}
        seller_ids = {r["seller_id"] for r in raw_items}
        translations = {r["product_category_name"]: r["product_category_name_english"] for r in _read_csv(zf, "product_category_name_translation.csv") if r.get("product_category_name")}
        products = {r["product_id"]: r for r in _read_csv(zf, "olist_products_dataset.csv") if r["product_id"] in product_ids}
        sellers = {r["seller_id"]: r for r in _read_csv(zf, "olist_sellers_dataset.csv") if r["seller_id"] in seller_ids}
        raw_payments = [r for r in _read_csv(zf, "olist_order_payments_dataset.csv") if r["order_id"] in order_ids]

    if output.exists(): output.unlink()
    with sqlite3.connect(output) as conn:
        conn.executescript(SCHEMA)
        conn.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?)", [(
            r["order_id"], r["customer_id"], r.get("order_status", "unknown"),
            r.get("order_purchase_timestamp") or None, r.get("order_approved_at") or None,
            r.get("order_delivered_carrier_date") or None, r.get("order_delivered_customer_date") or None,
            r.get("order_estimated_delivery_date") or None) for r in chosen])
        conn.executemany("INSERT INTO customers VALUES (?,?)", [(k, r["customer_unique_id"]) for k, r in customers.items()])
        conn.executemany("INSERT INTO products VALUES (?,?,?)", [(k, translations.get(r.get("product_category_name", ""), r.get("product_category_name") or "Unknown"), float(r["product_weight_g"]) if r.get("product_weight_g") else None) for k, r in products.items()])
        conn.executemany("INSERT INTO sellers VALUES (?,?)", [(k, r.get("seller_state", "")) for k, r in sellers.items()])
        conn.executemany("INSERT INTO order_items VALUES (?,?,?,?,?,?)", [(r["order_id"], int(r["order_item_id"]), r["product_id"], r["seller_id"], float(r["price"]), float(r["freight_value"])) for r in raw_items])
        conn.executemany("INSERT INTO payments VALUES (?,?,?,?,?)", [(r["order_id"], int(r["payment_sequential"]), r["payment_type"], int(r["payment_installments"]), float(r["payment_value"])) for r in raw_payments])
        conn.executemany("INSERT INTO dataset_meta VALUES (?,?)", [
            ("source", SOURCE_URL), ("source_license", "CC BY-NC-SA 4.0"),
            ("source_period", "2016-09-04 through 2018-10-17"), ("imported_at_utc", datetime.now(timezone.utc).isoformat()),
            ("subset_policy", f"latest {len(chosen)} orders by purchase timestamp, descending"),
            ("source_order_rows", str(len(order_rows))), ("imported_order_rows", str(len(chosen)))])
    return {"orders": len(chosen), "customers": len(customers), "items": len(raw_items), "payments": len(raw_payments), "products": len(products), "sellers": len(sellers)}


def list_orders(limit: int = 50, path: str | Path | None = None) -> list[dict[str, Any]]:
    if not 1 <= limit <= 200: raise ValueError("limit must be between 1 and 200")
    with connect(path) as conn:
        rows = conn.execute("SELECT order_id,status,purchase_at,customer_id FROM orders ORDER BY purchase_at DESC,order_id LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]


def lookup_order(order_id: str, path: str | Path | None = None) -> dict[str, Any] | None:
    with connect(path) as conn:
        row = conn.execute("""SELECT o.*, c.customer_unique_id,
          coalesce((SELECT sum(price+freight) FROM order_items i WHERE i.order_id=o.order_id),0) AS total,
          (SELECT count(*) FROM order_items i WHERE i.order_id=o.order_id) AS item_count,
          (SELECT count(*) FROM payments p WHERE p.order_id=o.order_id) AS payment_count
          FROM orders o LEFT JOIN customers c USING(customer_id) WHERE o.order_id=?""", (order_id,)).fetchone()
        return dict(row) if row else None


def query_customer(customer_id: str, path: str | Path | None = None) -> dict[str, Any] | None:
    with connect(path) as conn:
        row = conn.execute("SELECT customer_id,customer_unique_id FROM customers WHERE customer_id=? OR customer_unique_id=? LIMIT 1", (customer_id, customer_id)).fetchone()
        return dict(row) if row else None


def customer_orders(customer_unique_id: str, path: str | Path | None = None) -> list[dict[str, Any]]:
    with connect(path) as conn:
        rows = [dict(row) for row in conn.execute("""SELECT o.order_id,o.status,o.purchase_at,
          coalesce((SELECT sum(price+freight) FROM order_items i WHERE i.order_id=o.order_id),0) AS total
          FROM orders o JOIN customers c USING(customer_id) WHERE c.customer_unique_id=?
          ORDER BY o.purchase_at DESC LIMIT 20""", (customer_unique_id,))]
        return rows


def order_items(order_id: str, path: str | Path | None = None) -> list[dict[str, Any]]:
    with connect(path) as conn:
        return [dict(row) for row in conn.execute("""SELECT i.item_no,i.product_id,i.seller_id,i.price,i.freight,p.category,p.weight_g
          FROM order_items i LEFT JOIN products p USING(product_id) WHERE i.order_id=? ORDER BY i.item_no""", (order_id,))]


def order_payments(order_id: str, path: str | Path | None = None) -> list[dict[str, Any]]:
    with connect(path) as conn:
        return [dict(row) for row in conn.execute("SELECT sequence,kind,installments,value FROM payments WHERE order_id=? ORDER BY sequence", (order_id,))]


def product_details(product_id: str, path: str | Path | None = None) -> dict[str, Any] | None:
    with connect(path) as conn:
        row = conn.execute("SELECT product_id,category,weight_g FROM products WHERE product_id=?", (product_id,)).fetchone()
        return dict(row) if row else None


def seller_details(seller_id: str, path: str | Path | None = None) -> dict[str, Any] | None:
    with connect(path) as conn:
        row = conn.execute("SELECT seller_id,state FROM sellers WHERE seller_id=?", (seller_id,)).fetchone()
        return dict(row) if row else None


def delivery_status(order_id: str, path: str | Path | None = None) -> dict[str, Any] | None:
    order = lookup_order(order_id, path)
    if not order: return None
    actual, estimated = order.get("delivered_at"), order.get("estimated_at")
    outcome = "not_delivered_in_source" if not actual else "delivered_on_time" if not estimated or actual[:10] <= estimated[:10] else "delivered_late"
    return {"order_id": order_id, "source_status": order["status"], "carrier_handoff_at": order.get("carrier_at"), "delivered_at": actual, "estimated_at": estimated, "delivery_outcome": outcome, "source": "Olist order timestamps"}


def refund_eligibility(order_id: str, path: str | Path | None = None) -> dict[str, Any] | None:
    order = lookup_order(order_id, path)
    if not order: return None
    # Illustrative app rule only; Olist does not publish refund claims or policy.
    purchase = datetime.fromisoformat(order["purchase_at"]) if order.get("purchase_at") else None
    days = (datetime(2018, 10, 17, tzinfo=timezone.utc) - purchase.replace(tzinfo=timezone.utc)).days if purchase else None
    eligible = bool(order["status"] == "delivered" and days is not None and days <= 30)
    return {"order_id": order_id, "eligible_under_demo_policy": eligible, "simulated_policy": True,
        "rule": "Demo-only: delivered orders purchased within 30 days of the dataset's final date are eligible.",
        "source_facts": {"status": order["status"], "purchase_at": order.get("purchase_at"), "dataset_end_date": "2018-10-17"},
        "caveat": "Olist contains no refund claims or official refund policy; this is not an Olist fact."}


def execute_data_tool(name: str, args: dict[str, Any], path: str | Path | None = None) -> dict[str, Any]:
    """Dispatch an allowlisted dataset-backed, read-only tool."""
    schemas = {
        "customer.lookup": ("customer_id", query_customer),
        "customer.orders": ("customer_unique_id", customer_orders),
        "order.details": ("order_id", lookup_order),
        "order.items": ("order_id", order_items),
        "order.payments": ("order_id", order_payments),
        "order.delivery_status": ("order_id", delivery_status),
        "product.details": ("product_id", product_details),
        "seller.details": ("seller_id", seller_details),
        "refund.eligibility": ("order_id", refund_eligibility),
    }
    if name == "policy.lookup":
        if set(args) != {"topic"} or not isinstance(args.get("topic"), str) or not args["topic"].strip():
            return {"error": "Expected one non-empty string argument: topic."}
        return {"source": "simulated ContextGraph policy", "simulated": True, "topic": args["topic"],
            "text": "Demo policy: a delivered order purchased within 30 days of 2018-10-17 is eligible. This rule is illustrative and is not published by Olist."}
    if name not in schemas: return {"error": "Tool is not executable."}
    field, fn = schemas[name]
    expected = {field, "reason"} if name == "refund.eligibility" else {field}
    if set(args) != expected or any(not isinstance(args.get(key), str) or not args[key].strip() for key in expected):
        return {"error": f"Expected non-empty string arguments: {', '.join(sorted(expected))}."}
    result = fn(args[field], path)
    if result is None: return {"error": f"No matching source record for {field}={args[field]}"}
    return {"source": "Olist Brazilian E-Commerce Public Dataset", "data": result}
