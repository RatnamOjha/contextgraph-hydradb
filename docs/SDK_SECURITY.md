# SDK security and deployment boundaries

## Local trusted development

Run `./scripts/sdk-dev.sh` from the repository root. It uses the separate `.hydradb-sdk/` state directory and ports, skips the Olist demo seed, and opts into developer catalog writes for a loopback-only local backend. The normal `./scripts/dev.sh` serves the existing Olist demo with its own graph state.

The SDK server registration endpoint requires all of these conditions:

1. `CONTEXTGRAPH_ENV=development`.
2. `CONTEXTGRAPH_ENABLE_LOCAL_REGISTRATION=1`.
3. The caller address is loopback.
4. Render runtime markers are absent.

The API rejects registration with HTTP 403 if the guard fails. A regression test verifies the rejection happens before a HydraDB write. The guard is not a substitute for authentication.

## Credentials and private data

The current ContextGraph API does not authenticate callers or authorize catalog ownership. `ContextGraph(api_key=...)` only sends an `Authorization: Bearer` header; the API itself does not validate that value. A catalog identifier scopes records in HydraDB but is not secure multitenancy. Do not send private tool definitions, secrets, or customer data to the public demo.

The local MCP stdio command is executable code. `add_mcp_server()` starts exactly the configured executable and arguments. Use only commands and servers you trust. ContextGraph only initializes the MCP session and lists tool metadata; it does not call discovered business tools.

## Hosted deployment status

Render hosts the public Olist demonstration, not a private developer-catalog service. Catalog registration is disabled there, the demo API is unauthenticated, and Render Free storage is ephemeral. Do not enable public catalog writes by setting the local flags. Production onboarding needs user authentication, per-catalog authorization, secure secret management, durable storage, and operational limits before a developer can safely upload private catalogs.

## Safe testing

Run registration and MCP ingestion integration tests against the disposable HydraDB process started by `tests/test_sdk_catalog_integration.py`. They use temporary isolated graph state. Do not point tests at the shared Render graph or the ordinary `.hydradb/` Olist workspace. SDK retrieval does not call Groq; the Groq integration test is excluded from the SDK regression command.
