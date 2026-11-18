"""Async equivalent of the SDK's local MCP discovery and retrieval workflow."""
from pathlib import Path
import sys

from contextgraph import AsyncContextGraph


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "examples" / "commerce_mcp_server.py"


async def main() -> None:
    async with AsyncContextGraph("http://127.0.0.1:8001") as graph:
        report = await graph.add_mcp_server("commerce", sys.executable, [str(SERVER)],
            catalog="dummy-company", timeout=20)
        await graph.register_tools("dummy-company", [{
            "name": "commerce.check_refund", "server": "commerce",
            "description": "Check refund eligibility for an order",
            "input_schema": {"type": "object"}, "prerequisites": ["commerce.get_order"],
        }])
        context = await graph.retrieve("Check refund eligibility for order 123",
            catalog="dummy-company", max_tools=6)
        print(report)
        print([tool.id for tool in context.tools])
        print(context.dependencies)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
