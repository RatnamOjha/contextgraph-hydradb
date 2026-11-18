"""Pass retrieved schema metadata to an agent runtime owned by your application."""
from typing import Any

from contextgraph import ContextGraph


def retrieve_agent_tools(task: str) -> list[dict[str, Any]]:
    with ContextGraph("http://127.0.0.1:8001") as graph:
        context = graph.retrieve(task, catalog="dummy-company", max_tools=8)
        if not context.complete:
            raise RuntimeError(f"Incomplete tool context: {context.missing_dependencies}")
        # Adapt this list to your existing agent SDK. ContextGraph returns
        # definitions only; it never calls the functions.
        return [{
            "name": tool.id,
            "description": tool.description,
            "input_schema": tool.input_schema,
        } for tool in context.tools]


if __name__ == "__main__":
    for definition in retrieve_agent_tools("Check refund eligibility for order 123"):
        print(definition)
