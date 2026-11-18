import asyncio
from urllib.parse import quote

import httpx
import pytest

from backend.developer_catalog import register_catalog_tools, validate_catalog_registration
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

    async def query(self, query, parameters=None):
        self.calls.append((query, parameters or {}))
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


def test_registration_is_idempotent_upsert_and_schema_updates_are_stable_ids():
    first, second = FakeHydra(), FakeHydra()
    original = [{"name": "get_order", "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}}}]
    changed = [{"name": "get_order", "input_schema": {"type": "object", "properties": {"id": {"type": "integer"}}}}]
    one = asyncio.run(register_catalog_tools(first, "commerce", original))
    again = asyncio.run(register_catalog_tools(second, "commerce", original))
    refreshed = asyncio.run(register_catalog_tools(FakeHydra(), "commerce", changed))
    assert one["registered"][0]["id"] == refreshed["registered"][0]["id"]
    assert one["fingerprint"] == again["fingerprint"]
    assert one["fingerprint"] != refreshed["fingerprint"]
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

