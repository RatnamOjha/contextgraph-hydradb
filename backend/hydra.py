"""Small typed HTTP client for HydraDB's documented JSON query endpoint."""
from __future__ import annotations

import hashlib
import os
import time
from typing import Any

import httpx


class HydraError(RuntimeError):
    pass


class HydraClient:
    def __init__(self) -> None:
        self.url = os.getenv("HYDRA_URL", "http://127.0.0.1:18443").rstrip("/")
        self.graph = os.getenv("HYDRA_GRAPH", "default")
        self.namespace = os.getenv("HYDRA_NAMESPACE", "contextgraph")
        self.cell = os.getenv("HYDRA_CELL", "cell-0")
        self.token = os.getenv("HYDRA_TOKEN", "contextgraph-local-development-token-32-bytes")
        self.timeout = float(os.getenv("HYDRA_TIMEOUT_SECONDS", "3"))
        self.endpoint = f"{self.url}/v1/graphs/{self.graph}/query"
        self._client: httpx.AsyncClient | None = None

    async def open(self) -> None:
        self._client = httpx.AsyncClient(timeout=self.timeout, limits=httpx.Limits(max_connections=64, max_keepalive_connections=16))

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def id_for(kind: str, key: str) -> int:
        raw = hashlib.blake2b(f"{kind}:{key}".encode(), digest_size=8).digest()
        return int.from_bytes(raw, "big") & ((1 << 62) - 1) or 1

    async def query(self, cypher: str, parameters: dict[str, Any] | None = None) -> tuple[list[str], list[list[Any]], float]:
        body = {"cell_id": self.cell, "query": cypher}
        if parameters:
            body["parameters"] = parameters
        headers = {
            "Authorization": f"Bearer {self.token}",
            "X-Graph-Namespace": self.namespace,
            "Content-Type": "application/json",
        }
        start = time.perf_counter()
        try:
            if self._client is None:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.post(self.endpoint, json=body, headers=headers)
            else:
                response = await self._client.post(self.endpoint, json=body, headers=headers)
            if response.is_error:
                raise HydraError(f"HTTP {response.status_code}: {response.text}")
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise HydraError(f"HydraDB query failed: {exc}") from exc
        elapsed_ms = (time.perf_counter() - start) * 1000
        if "error" in payload:
            raise HydraError(payload["error"].get("message", "HydraDB returned an error"))
        rows = [[decode_cell(cell) for cell in row] for row in payload.get("rows", [])]
        return payload.get("columns", []), rows, elapsed_ms


def decode_cell(cell: Any) -> Any:
    if not isinstance(cell, dict) or "type" not in cell:
        return cell
    value = cell.get("value")
    if cell["type"] == "list" and isinstance(value, list):
        return [decode_cell(item) for item in value]
    return value


def cypher_string(value: str) -> str:
    """Quote internal catalog strings as Cypher string literals."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n") + "'"
