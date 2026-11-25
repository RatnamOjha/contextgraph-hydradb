"""Evaluate retrieval labels against graph and flat lexical strategies."""
import json

from contextgraph import ContextGraph


TASKS = [
    {"task": "Check refund eligibility for order 123",
     "required_tools": ["dummy-company.commerce.check_refund"]},
    {"task": "Read the status for order 123",
     "required_tools": ["dummy-company.commerce.get_order"]},
    {"task": "List recorded payments for order 123",
     "required_tools": ["dummy-company.commerce.list_payments"]},
]


def main() -> None:
    with ContextGraph("http://127.0.0.1:8001") as graph:
        report = graph.evaluate("dummy-company", TASKS, max_tools=6)
        print(json.dumps(report.model_dump(), indent=2))


if __name__ == "__main__":
    main()
