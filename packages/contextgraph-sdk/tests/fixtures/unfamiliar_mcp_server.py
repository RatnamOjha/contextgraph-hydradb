"""Independent stdio MCP catalog with two unfamiliar business workflows."""
from mcp.server.mcpserver import MCPServer

server = MCPServer(name="unfamiliar-commerce", version="1.0.0")


@server.tool()
async def get_order(order_id: str) -> dict:
    """Read a historical order by its opaque identifier."""
    return {"order_id": order_id}


@server.tool()
async def check_refund(order_id: str, reason: str) -> dict:
    """Check whether an order qualifies for a refund."""
    return {"eligible": False}


@server.tool()
async def list_replacement_parts(machine_model: str) -> list[dict]:
    """Find replacement parts compatible with a machine model."""
    return []


@server.tool()
async def check_part_stock(part_code: str, warehouse: str | None = None) -> dict:
    """Check stock for a replacement part."""
    return {"available": 0}


@server.tool()
async def schedule_technician(service_area: str, issue_code: str) -> dict:
    """Find a service technician for a reported equipment issue."""
    return {"scheduled": False}


if __name__ == "__main__":
    server.run()
