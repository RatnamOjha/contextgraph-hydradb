"""Deterministic support-tool catalog and evaluation fixtures."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re


@dataclass(frozen=True)
class Tool:
    key: str
    name: str
    description: str
    server: str
    skill: str
    input_schema: str
    dependencies: tuple[str, ...] = ()
    entities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Store valid MCP inputSchema JSON rather than shorthand labels. The
        # compact declarations below are expanded once when fixtures load.
        schema = self.input_schema.strip()
        if schema.startswith("{") and '"type"' in schema:
            return
        properties: dict[str, dict[str, str]] = {}
        required: list[str] = []
        inner = schema.removeprefix("{").removesuffix("}")
        for field in inner.split(","):
            match = re.fullmatch(r"\s*([A-Za-z_][\w-]*)(\?)?\s*:\s*(string|integer|number|boolean)\s*", field)
            if not match:
                continue
            name, optional, kind = match.groups()
            properties[name] = {"type": kind}
            if optional is None:
                required.append(name)
        normalized = {"type": "object", "properties": properties, "additionalProperties": False}
        if required:
            normalized["required"] = required
        object.__setattr__(self, "input_schema", json.dumps(normalized, separators=(",", ":")))


SERVERS = {
    "support-mcp": "Support Desk MCP",
    "commerce-mcp": "Commerce MCP",
    "billing-mcp": "Billing MCP",
    "shipping-mcp": "Shipping MCP",
    "olist-data": "Olist Historical Data Tools",
    "contextgraph-simulated": "ContextGraph Simulated Policy",
}

CORE_TOOLS = [
    Tool("ticket.lookup", "Find support ticket", "Retrieve a support ticket by ticket ID and return its customer reference and issue details.", "support-mcp", "support_triage", "{ticket_id: string}", entities=("ticket",)),
    Tool("customer.resolve", "Resolve customer", "Resolve a customer ID, email address, or ticket reference to a canonical customer record.", "support-mcp", "customer_context", "{reference: string}", entities=("customer",)),
    Tool("customer.profile", "Read customer profile", "Get customer tier, region, account status, and support history by canonical customer ID.", "support-mcp", "customer_context", "{customer_id: string}", dependencies=("customer.resolve",), entities=("customer",)),
    Tool("orders.list", "List recent orders", "List recent commerce orders for a customer, newest first, including fulfillment and payment state.", "commerce-mcp", "refund_review", "{customer_id: string, limit?: integer}", dependencies=("customer.resolve",), entities=("customer", "order")),
    Tool("order.get", "Get order details", "Retrieve line items, delivery status, and total for a canonical order ID.", "commerce-mcp", "refund_review", "{order_id: string}", dependencies=("orders.list",), entities=("order",)),
    Tool("refund.eligibility", "Check refund eligibility", "Check refund policy using order ID, delivery date, amount, and prior refund state.", "commerce-mcp", "refund_review", "{order_id: string, reason: string}", dependencies=("order.get",), entities=("order",)),
    Tool("refund.create", "Create refund", "Issue an approved refund for an eligible order and return the refund receipt.", "commerce-mcp", "refund_review", "{order_id: string, amount: number, idempotency_key: string}", dependencies=("refund.eligibility",), entities=("order",)),
    Tool("invoice.lookup", "Find invoice", "Retrieve invoices and outstanding balance for a customer account.", "billing-mcp", "billing_review", "{customer_id: string}", dependencies=("customer.resolve",), entities=("customer", "invoice")),
    Tool("payment.history", "Read payment history", "List payment events and failed charges for a customer account.", "billing-mcp", "billing_review", "{customer_id: string}", dependencies=("customer.resolve",), entities=("customer",)),
    Tool("subscription.status", "Check subscription", "Read subscription status, renewal date, and plan for a customer.", "billing-mcp", "billing_review", "{customer_id: string}", dependencies=("customer.resolve",), entities=("customer",)),
    Tool("shipment.track", "Track shipment", "Look up the latest carrier scan and estimated delivery for an order.", "shipping-mcp", "delivery_investigation", "{order_id: string}", dependencies=("orders.list",), entities=("order",)),
    Tool("return.status", "Check return status", "Retrieve a return authorization and warehouse receipt state for an order.", "commerce-mcp", "delivery_investigation", "{order_id: string}", dependencies=("order.get",), entities=("order",)),
    Tool("policy.search", "Search support policy", "Find the current policy excerpt for a support issue category and region.", "support-mcp", "support_triage", "{category: string, region?: string}"),
    Tool("ticket.reply", "Draft ticket reply", "Create a draft response on a support ticket; does not send without confirmation.", "support-mcp", "support_triage", "{ticket_id: string, message: string}", dependencies=("ticket.lookup",), entities=("ticket",)),
    Tool("customer.note", "Append customer note", "Append a short internal note to the canonical customer support record.", "support-mcp", "customer_context", "{customer_id: string, note: string}", dependencies=("customer.resolve",), entities=("customer",)),
    Tool("payment.reverse", "Reverse payment", "Reverse a captured payment after policy approval; requires an idempotency key.", "billing-mcp", "billing_review", "{payment_id: string, idempotency_key: string}", dependencies=("payment.history",)),
    Tool("order.cancel", "Cancel order", "Cancel an unfulfilled order and release its payment authorization.", "commerce-mcp", "delivery_investigation", "{order_id: string}", dependencies=("order.get",)),
    Tool("shipment.address", "Check delivery address", "Read the destination address associated with a shipment without changing it.", "shipping-mcp", "delivery_investigation", "{order_id: string}", dependencies=("orders.list",)),
]

SKILLS = {
    "support_triage": ("Support triage", "Investigate a customer support ticket and prepare an evidence-based reply."),
    "customer_context": ("Customer context", "Resolve a customer identity and retrieve the account context needed for support."),
    "order_support": ("Order support", "Inspect a historical order, its line items, and its related business records."),
    "refund_review": ("Refund review", "Check order and policy evidence before deciding refund eligibility."),
    "billing_review": ("Billing review", "Investigate invoices, subscription state, and payment history."),
    "delivery_investigation": ("Delivery investigation", "Trace order fulfillment, carrier events, and return status."),
    "product_research": ("Product and seller research", "Inspect product category, weight, and seller details for order items."),
    "catalog_utilities": ("Catalog utilities", "General read-only helpers not tied to the active support workflow."),
}

DATA_TOOLS = [
    Tool("customer.lookup", "Look up customer", "Find an anonymized Olist customer ID and its stable customer reference. Omits customer location and contact data.", "olist-data", "customer_context", "{customer_id: string}"),
    Tool("customer.orders", "List customer orders", "List historical source orders for the stable customer reference returned by customer.lookup.", "olist-data", "customer_context", "{customer_unique_id: string}", dependencies=("customer.lookup",)),
    Tool("order.details", "Read order details", "Read source order status, timestamps, total item value, item count, and associated anonymized customer IDs.", "olist-data", "order_support", "{order_id: string}"),
    Tool("order.items", "Read order items", "Read source line items with product and seller IDs, price, freight, category, and product weight.", "olist-data", "product_research", "{order_id: string}", dependencies=("order.details",)),
    Tool("order.payments", "Read order payments", "Read source payment types, installment counts, and values for an order.", "olist-data", "billing_review", "{order_id: string}", dependencies=("order.details",)),
    Tool("order.delivery_status", "Read delivery status", "Derive delivery timing only from source carrier, delivered, and estimated timestamps.", "olist-data", "delivery_investigation", "{order_id: string}", dependencies=("order.details",)),
    Tool("product.details", "Read product details", "Read a product's source category and weight. Product descriptions are not present in the dataset.", "olist-data", "product_research", "{product_id: string}", dependencies=("order.items",)),
    Tool("seller.details", "Read seller details", "Read a seller's anonymized source ID and state only.", "olist-data", "product_research", "{seller_id: string}", dependencies=("order.items",)),
    Tool("policy.lookup", "Read demo refund policy", "Return the named ContextGraph demo policy; this is simulated app content, not an Olist business rule.", "contextgraph-simulated", "refund_review", "{topic: string}"),
    Tool("refund.eligibility", "Evaluate demo refund policy", "Apply the explicitly simulated ContextGraph demo rule to source order facts; this is not an Olist policy or historical refund record.", "contextgraph-simulated", "refund_review", "{order_id: string, reason: string}", dependencies=("order.details",)),
]

TASKS = {
    "refund": {
        "title": "Refund eligibility from a support ticket",
        "task": "Ticket T-2049: find the customer, retrieve their recent order, and determine whether the order qualifies for a refund. Do not issue a refund.",
        "skill": "refund_review",
        "skills": ["support_triage", "customer_context", "refund_review"],
        "required": ["ticket.lookup", "customer.resolve", "orders.list", "order.get", "refund.eligibility"],
        "ticket": "T-2049",
        "customer": "C-1042",
        "order": "O-8891",
        "eligible": True,
    },
    "billing": {
        "title": "Explain a disputed charge",
        "task": "Customer C-1042 says the latest charge looks wrong. Find the account, invoice, and payment events and summarize what happened.",
        "skill": "billing_review",
        "skills": ["customer_context", "billing_review"],
        "required": ["customer.resolve", "customer.profile", "invoice.lookup", "payment.history"],
        "ticket": "T-2049",
        "customer": "C-1042",
        "order": "O-8891",
        "eligible": False,
    },
    "delivery": {
        "title": "Investigate a late delivery",
        "task": "Support ticket T-2049 reports a late package. Resolve the customer, find the order, and check the carrier scan and return state.",
        "skill": "delivery_investigation",
        "skills": ["support_triage", "customer_context", "delivery_investigation"],
        "required": ["ticket.lookup", "customer.resolve", "orders.list", "order.get", "shipment.track", "return.status"],
        "ticket": "T-2049",
        "customer": "C-1042",
        "order": "O-8891",
        "eligible": False,
    },
}

# Training prompts use different text and entity IDs from TASKS. Only their
# observed successful route feeds back into the shared TaskType relationships.
TRAINING_TASKS = {
    "refund": {"prompt": "RMA case R-771: verify whether the returned purchase meets refund policy.", "skill": "refund_review", "ticket": "T-7711", "customer": "C-771", "order": "O-771", "required_tools": ["ticket.lookup", "order.get", "refund.eligibility"]},
    "billing": {"prompt": "Account A-552 has a charge question; reconcile the posted invoice with payment events.", "skill": "billing_review", "ticket": "T-5521", "customer": "C-552", "order": "O-552", "required_tools": ["invoice.lookup", "payment.history", "customer.profile"]},
    "delivery": {"prompt": "Case S-884: trace the delayed parcel and inspect its return authorization.", "skill": "delivery_investigation", "ticket": "T-8841", "customer": "C-884", "order": "O-884", "required_tools": ["ticket.lookup", "order.get", "shipment.track", "return.status"]},
}

# Unseen wording and identifiers: includes uncued IDs, missing IDs, and prompts
# where tool descriptions overlap (e.g. return/charge/lookup).
EVALUATION_CASES = [
    {"name": "refund_rephrase", "scenario": "refund", "task": "For ticket T-2049, can policy cover the purchase? Check evidence only; do not send money.", "entity_expected": True},
    {"name": "refund_unknown_ticket", "scenario": "refund", "task": "Customer C-7777 wants their money back for an item. Check refund eligibility, no action.", "entity_expected": False},
    {"name": "billing_unfamiliar", "scenario": "billing", "task": "C-1042 does not recognize a debit. Reconcile the statement and captured or failed transaction records.", "entity_expected": True},
    {"name": "billing_no_identifier", "scenario": "billing", "task": "Please explain whether the latest account charge actually settled and whether anything remains due.", "entity_expected": False},
    {"name": "delivery_overlapping_return", "scenario": "delivery", "task": "The parcel for ticket T-2049 is delayed. Look up the order, its carrier scan, and whether a return was opened.", "entity_expected": True},
    {"name": "delivery_unseen_order", "scenario": "delivery", "task": "Track order O-5555 and check the warehouse return status; do not cancel the purchase.", "entity_expected": False},
    {"name": "refund_blended_tool_words", "scenario": "refund", "task": "Ticket T-2049: inspect the order and policy, but only determine eligibility; never create a refund.", "entity_expected": True},
    {"name": "billing_overlapping_descriptions", "scenario": "billing", "task": "Look up invoice and payment history for customer C-1042; explain the disputed charge without reversing it.", "entity_expected": True},
    {"name": "delivery_missing_entity", "scenario": "delivery", "task": "A shipment is late and a return may be pending. Find the carrier event and return authorization.", "entity_expected": False},
]

def make_catalog(size: int = 20, scope: str = "combined") -> list[Tool]:
    """Return the dummy-company catalog or the combined company/Olist catalog."""
    if scope == "company":
        return list(CORE_TOOLS)
    if scope != "combined":
        raise ValueError(f"Unknown catalog scope: {scope}")
    # Keep the company definition when a combined catalog has a colliding key.
    unique: dict[str, Tool] = {}
    for tool in [*CORE_TOOLS, *DATA_TOOLS]:
        unique.setdefault(tool.key, tool)
    return list(unique.values())
