"""Read-only dummy commerce MCP catalog used by the ContextGraph SDK examples."""
from mcp.server.mcpserver import MCPServer

server = MCPServer(name="dummy-commerce", version="1.0.0")


@server.tool()
async def get_order(order_id: str) -> dict:
    """Read a fictional order using its order identifier."""
    return {"order_id": order_id, "status": "processing"}


@server.tool()
async def get_customer(customer_id: str) -> dict:
    """Read a fictional customer's account profile."""
    return {"customer_id": customer_id, "tier": "standard"}


@server.tool()
async def check_refund(order_id: str, reason: str) -> dict:
    """Check whether a fictional order meets the refund policy."""
    return {"order_id": order_id, "eligible": False, "reason": reason}


@server.tool()
async def list_payments(order_id: str) -> list[dict]:
    """List fictional payments recorded for an order."""
    return []


@server.tool()
async def get_refund_policy(region: str) -> dict:
    """Read the fictional refund policy for a region."""
    return {"region": region, "days": 30}


if __name__ == "__main__":
    server.run()
