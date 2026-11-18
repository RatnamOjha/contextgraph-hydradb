"""Discover a local MCP catalog, declare a prerequisite, and retrieve context."""
from pathlib import Path
import sys

from contextgraph import ContextGraph


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "examples" / "commerce_mcp_server.py"


def main() -> None:
    with ContextGraph("http://127.0.0.1:8001") as graph:
        report = graph.add_mcp_server(
            name="commerce",
            command=sys.executable,
            args=[str(SERVER)],
            catalog="dummy-company",
            timeout=20,
        )
        # MCP tool metadata does not declare prerequisites; add the real
        # relationship explicitly instead of guessing from descriptions.
        graph.register_tools("dummy-company", [{
            "name": "commerce.check_refund",
            "server": "commerce",
            "description": "Check whether an order qualifies for a refund.",
            "input_schema": {"type": "object", "properties": {
                "order_id": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["order_id", "reason"]},
            "prerequisites": ["commerce.get_order"],
        }])
        context = graph.retrieve(
            task="Check refund eligibility for order 123",
            catalog="dummy-company",
            max_tools=6,
        )
        print(f"Ingested: {report.discovered_tool_count} tools")
        print("Selected tools:")
        for tool in context.tools:
            print(f"- {tool.id}: {tool.description}")
            print(f"  schema: {tool.input_schema}")
            print(f"  why: {context.selection_reasons.get(tool.id, [])}")
        print(f"Dependencies: {context.dependencies}")
        print(f"Complete: {context.complete}; execution performed: {context.execution_performed}")


if __name__ == "__main__":
    main()
