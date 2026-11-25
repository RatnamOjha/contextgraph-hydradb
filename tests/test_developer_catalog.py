import asyncio
from urllib.parse import quote

import httpx
import pytest

from backend.developer_catalog import catalog_fingerprint, register_catalog_tools, validate_catalog_registration
from contextgraph import ContextGraph


def definitions():
    return [
        {"name": "check_refund", "description": "Check eligibility", "input_schema": {"type": "object"},
         "server": "commerce", "prerequisites": ["get_order"]},
        {"name": "get_order", "description": "Get an order", "input_schema": {"type": "object"},
         "server": "commerce", "prerequisites": []},
    ]


class FakeHydra:
    def __init__(self):
        self.calls = []
        self.provided_by = set()

    async def query(self, query, parameters=None):
        parameters = parameters or {}
        self.calls.append((query, parameters))
        if "PROVIDED_BY" not in query:
            return [], [], 0.1
        if "CREATE (a {id: row.source})-[:PROVIDED_BY]->(b {id: row.target})" in query:
            self.provided_by.update((int(row["source"]), int(row["target"])) for row in parameters.get("rows", []))
        if "DELETE r" in query:
            self.provided_by = {edge for edge in self.provided_by if edge[0] != int(parameters["id"])}
        if "RETURN s.id AS id" in query:
            source = int(parameters["id"])
            return [], [[target] for edge_source, target in sorted(self.provided_by) if edge_source == source], 0.1
        if "RETURN a.id LIMIT 1" in query:
            edge = (int(parameters["source"]), int(parameters["target"]))
            return [], [[edge[0]]] if edge in self.provided_by else [], 0.1
        return [], [], 0.1


def test_registration_is_scoped_explicit_and_order_independent():
    normalized = validate_catalog_registration("commerce", definitions())
    assert [tool["name"] for tool in normalized] == ["check_refund", "get_order"]
    db = FakeHydra()
    result = asyncio.run(register_catalog_tools(db, "commerce", definitions()))
    assert result["registered"][0]["id"] == "commerce.check_refund"
    assert result["catalog_version"] == result["fingerprint"][:16]
    assert any("t.catalog=row.catalog" in query for query, _ in db.calls)
    assert any("CREATE (a {id: row.source})-[:DEPENDS_ON]->(b {id: row.target})" in query for query, _ in db.calls)
    assert all("DETACH DELETE" not in query for query, _ in db.calls)


def test_registration_rejects_implicit_or_cross_catalog_dependencies():
    tools = [{"name": "refund", "prerequisites": ["other.get_order"]}]
    with pytest.raises(ValueError, match="unknown prerequisites"):
        validate_catalog_registration("commerce", tools)
    with pytest.raises(ValueError, match="unknown prerequisites"):
        validate_catalog_registration("commerce", [{"name": "refund", "prerequisites": ["get_order"]}])


def test_omitted_prerequisites_are_distinct_from_an_explicit_clear():
    omitted = validate_catalog_registration("commerce", [{"name": "refund"}])[0]
    clear = validate_catalog_registration("commerce", [{"name": "refund", "prerequisites": []}])[0]
    assert omitted["prerequisites"] is None
    assert clear["prerequisites"] == []

    from backend.app import RegisterToolDefinition
    assert "prerequisites" not in RegisterToolDefinition(name="refund").model_dump(exclude_unset=True)
    assert RegisterToolDefinition(name="refund", prerequisites=[]).model_dump(exclude_unset=True)["prerequisites"] == []


def test_catalog_fingerprint_covers_complete_effective_metadata_and_dependencies():
    tools = [
        {"id": "commerce.get_order", "name": "get_order", "description": "Read an order",
         "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}},
         "server": "commerce", "prerequisites": []},
        {"id": "commerce.check_refund", "name": "check_refund", "description": "Check a refund",
         "input_schema": {"type": "object"}, "server": "commerce",
         "prerequisites": ["commerce.get_order"]},
    ]
    fingerprint = catalog_fingerprint(tools)
    assert catalog_fingerprint(list(reversed(tools))) == fingerprint

    changed_schema = [dict(tool) for tool in tools]
    changed_schema[0] = {**changed_schema[0], "input_schema": {"type": "object", "properties": {"id": {"type": "integer"}}}}
    assert catalog_fingerprint(changed_schema) != fingerprint

    changed_dependencies = [dict(tool) for tool in tools]
    changed_dependencies[1] = {**changed_dependencies[1], "prerequisites": []}
    assert catalog_fingerprint(changed_dependencies) != fingerprint


def test_registration_is_idempotent_upsert_and_schema_updates_are_stable_ids():
    first, second = FakeHydra(), FakeHydra()
    original = [{"name": "get_order", "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}}}]
    changed = [{"name": "get_order", "input_schema": {"type": "object", "properties": {"id": {"type": "integer"}}}}]
    one = asyncio.run(register_catalog_tools(first, "commerce", original))
    again = asyncio.run(register_catalog_tools(second, "commerce", original))
    refreshed = asyncio.run(register_catalog_tools(FakeHydra(), "commerce", changed))
    assert one["registered"][0]["id"] == refreshed["registered"][0]["id"]
    assert any("MERGE (t {id: row.tool_id})" in query for query, _ in second.calls)


def test_sdk_register_tools_posts_once_to_catalog_endpoint():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"catalog": "commerce", "registered": []})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with ContextGraph("https://example.test", retries=3, http_client=client) as cg:
        result = cg.register_tools("commerce", [{"name": "get_order"}])
    assert result["catalog"] == "commerce"
    assert len(calls) == 1
    assert calls[0].method == "POST"
    assert calls[0].url.path == f"/api/v1/catalogs/{quote('commerce', safe='')}/tools"


def test_registration_route_is_disabled_without_explicit_local_opt_in(monkeypatch):
    from starlette.requests import Request
    from backend.app import _local_catalog_registration_allowed

    monkeypatch.delenv("CONTEXTGRAPH_ENV", raising=False)
    monkeypatch.delenv("CONTEXTGRAPH_ENABLE_LOCAL_REGISTRATION", raising=False)
    scope = {"type": "http", "method": "POST", "path": "/", "headers": [],
             "client": ("127.0.0.1", 1000), "server": ("127.0.0.1", 8000), "scheme": "http"}
    assert _local_catalog_registration_allowed(Request(scope)) is False
    monkeypatch.setenv("CONTEXTGRAPH_ENV", "development")
    monkeypatch.setenv("CONTEXTGRAPH_ENABLE_LOCAL_REGISTRATION", "1")
    assert _local_catalog_registration_allowed(Request(scope)) is True
    monkeypatch.setenv("RENDER", "true")
    assert _local_catalog_registration_allowed(Request(scope)) is False


def test_registration_route_rejects_unauthorized_write_without_touching_hydradb(monkeypatch):
    from starlette.requests import Request
    from fastapi import HTTPException
    import backend.app as app_module

    class MustNotWrite:
        async def query(self, *args, **kwargs):
            raise AssertionError("unauthorized registration reached HydraDB")

    monkeypatch.delenv("CONTEXTGRAPH_ENV", raising=False)
    monkeypatch.delenv("CONTEXTGRAPH_ENABLE_LOCAL_REGISTRATION", raising=False)
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("RENDER_SERVICE_ID", raising=False)
    monkeypatch.setattr(app_module, "db", MustNotWrite())
    scope = {"type": "http", "method": "POST", "path": "/", "headers": [],
             "client": ("198.51.100.10", 1000), "server": ("example.test", 8000), "scheme": "http"}

    with pytest.raises(HTTPException) as error:
        asyncio.run(app_module.register_tools(
            "commerce", Request(scope), app_module.RegisterToolsRequest(tools=[{"name": "get_order"}])
        ))
    assert error.value.status_code == 403

