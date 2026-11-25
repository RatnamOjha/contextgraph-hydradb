import pytest

from backend.catalog_retrieval import retrieve_catalog_context


TOOLS = [
    {"key": "ops.factory.get_order", "catalog": "ops", "name": "Get order",
     "description": "Read an order and customer record", "schema": '{"type":"object"}',
     "server": "factory", "provenance": "developer_registered"},
    {"key": "ops.factory.check_refund", "catalog": "ops", "name": "Check refund eligibility",
     "description": "Evaluate the refund policy for an order", "schema": '{"type":"object"}',
     "server": "factory", "provenance": "developer_registered"},
    {"key": "ops.warehouse.check_stock", "catalog": "ops", "name": "Check stock",
     "description": "Check replacement part stock at a warehouse", "schema": '{"type":"object"}',
     "server": "warehouse", "provenance": "developer_registered"},
    {"key": "ops.warehouse.find_part", "catalog": "ops", "name": "Find replacement part",
     "description": "Search compatible repair parts", "schema": '{"type":"object"}',
     "server": "warehouse", "provenance": "developer_registered"},
]
EDGES = [
    ("ops.factory.check_refund", "ops.factory.get_order"),
    ("ops.factory.get_order", "ops.factory.check_refund"),  # cycle
    ("ops.warehouse.check_stock", "ops.warehouse.missing_supplier"),
]
SERVERS = [(tool["key"], tool["server"]) for tool in TOOLS]
CATALOG_EDGES = [(tool["key"], "ops") for tool in TOOLS]


def retrieve(task, **kwargs):
    return retrieve_catalog_context(catalog="ops", task=task, tools=TOOLS,
        dependencies=EDGES, servers=SERVERS, catalog_edges=CATALOG_EDGES, **kwargs)


def test_retrieval_ranks_unfamiliar_descriptions_and_expands_explicit_prerequisites():
    result = retrieve("Determine refund eligibility for this order", max_tools=4)
    ids = {node["id"] for node in result["nodes"] if node["type"] == "Tool"}
    assert "ops.factory.check_refund" in ids
    assert "ops.factory.get_order" in ids
    assert any(edge["type"] == "DEPENDS_ON" for edge in result["edges"])
    assert result["complete"] is True  # cycle terminates; both explicit tools are present
    assert result["nodes"][0]["type"] == "Catalog"
    assert all(edge["type"] in {"DEPENDS_ON", "PROVIDED_BY", "IN_CATALOG"} for edge in result["edges"])


def test_unrelated_workflow_stays_out_and_context_is_bounded():
    result = retrieve("Find replacement part compatibility", max_tools=2)
    ids = {node["id"] for node in result["nodes"] if node["type"] == "Tool"}
    assert "ops.warehouse.find_part" in ids
    assert "ops.factory.check_refund" not in ids
    assert len(ids) <= 2


def test_overlapping_tool_descriptions_prefer_the_more_specific_catalog_match():
    result = retrieve("Find a compatible replacement part", max_tools=4)
    ids = {node["id"] for node in result["nodes"] if node["type"] == "Tool"}
    assert "ops.warehouse.find_part" in ids
    assert "ops.warehouse.check_stock" not in ids


def test_missing_prerequisite_and_exclusion_are_explicitly_incomplete():
    result = retrieve("Check stock for the warehouse", max_tools=4)
    assert "ops.warehouse.missing_supplier" in result["missing_dependencies"]
    assert result["complete"] is False
    excluded = retrieve("Determine refund eligibility for this order", max_tools=4,
        exclude_tool_ids=["ops.factory.get_order"])
    assert "ops.factory.get_order" in excluded["missing_dependencies"]
    assert excluded["complete"] is False


def test_known_context_is_validated_and_does_not_trigger_dependency_claims():
    result = retrieve("Determine refund eligibility for this order", max_tools=3,
        known_tool_ids=["ops.factory.get_order"])
    known = next(node for node in result["nodes"] if node.get("id") == "ops.factory.get_order")
    assert known["metadata"]["known_context"] is True
    assert known["type"] == "KnownTool"
    assert "ops.factory.get_order" not in {node["id"] for node in result["nodes"] if node["type"] == "Tool"}
    assert result["complete"] is True
    known_nodes = [node for node in result["nodes"] if node.get("id") == "ops.factory.get_order"]
    assert len(known_nodes) == 1
    assert known_nodes[0]["type"] == "KnownTool"
    assert result["bounded"]["selected_tools"] == 1
    assert len([node for node in result["nodes"] if node.get("type") == "Tool"]) == 1
    with pytest.raises(ValueError, match="must exist in the selected catalog"):
        retrieve("Check refund policy for order", known_tool_ids=["other.get_order"])


def test_max_tool_count_and_task_lengths_are_validated():
    with pytest.raises(ValueError, match="max_tools"):
        retrieve("Check refund eligibility", max_tools=51)
    with pytest.raises(ValueError, match="8–1200"):
        retrieve("short")


def test_tool_cap_reports_candidates_omitted_by_the_bound():
    result = retrieve("Check refund eligibility and replacement part stock", max_tools=1)
    tools = [node for node in result["nodes"] if node["type"] == "Tool"]
    assert len(tools) <= 1
    assert result["omitted_tool_ids"]
    assert result["complete"] is False

