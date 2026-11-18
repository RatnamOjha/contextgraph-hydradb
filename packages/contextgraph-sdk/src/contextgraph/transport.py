"""Shared HTTP mechanics for sync and async SDK clients."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from .exceptions import ContextGraphError
from .models import ContextGraphErrorInfo

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def validate_base_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("url must be an absolute http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("url must not contain credentials, query parameters, or a fragment")
    return url.rstrip("/")


def _error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError):
        return f"HTTP {response.status_code}"
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, dict):
        detail = detail.get("message") or detail.get("error")
    if not isinstance(detail, str) or not detail.strip():
        return f"HTTP {response.status_code}"
    # Bound server-provided text so an error cannot echo an oversized payload.
    return detail.strip()[:300]


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    if response is not None:
        raw = response.headers.get("Retry-After", "")
        try:
            return max(0.0, min(1.0, float(raw)))
        except ValueError:
            pass
    return min(.1 * (2 ** attempt), .5)


class _BaseTransport:
    def __init__(self, url: str, timeout: float, retries: int, api_key: str | None) -> None:
        self.base_url = validate_base_url(url)
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if not 0 <= retries <= 3:
            raise ValueError("retries must be between 0 and 3")
        self.timeout = timeout
        self.retries = retries
        self._headers = {"Accept": "application/json"}
        if api_key:
            # The current demo API does not enforce API keys. Sending this
            # header is only useful with a separately configured gateway.
            self._headers["Authorization"] = f"Bearer {api_key}"

    @staticmethod
    def _parse(response: httpx.Response, method: str, path: str) -> dict[str, Any]:
        if response.is_error:
            raise ContextGraphError(ContextGraphErrorInfo(
                message=f"ContextGraph API returned {_error_detail(response)}",
                status_code=response.status_code, method=method, path=path,
                retryable=response.status_code in _RETRYABLE_STATUS))
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ContextGraphError(ContextGraphErrorInfo(
                message="ContextGraph API returned invalid JSON", status_code=response.status_code,
                method=method, path=path)) from exc
        if not isinstance(payload, dict):
            raise ContextGraphError(ContextGraphErrorInfo(
                message="ContextGraph API returned an unexpected response shape", status_code=response.status_code,
                method=method, path=path))
        return payload


class SyncTransport(_BaseTransport):
    def __init__(self, url: str, timeout: float = 15.0, retries: int = 1, api_key: str | None = None,
                 http_client: httpx.Client | None = None) -> None:
        super().__init__(url, timeout, retries, api_key)
        self._client: httpx.Client | None = http_client or httpx.Client(timeout=timeout, follow_redirects=False)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _ensure_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout, follow_redirects=False)
        return self._client

    def get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        client = self._ensure_client()
        url = f"{self.base_url}{path}"
        for attempt in range(self.retries + 1):
            response = None
            try:
                response = client.get(url, params=params, headers=self._headers)
                if response.status_code in _RETRYABLE_STATUS and attempt < self.retries:
                    time.sleep(_retry_delay(response, attempt))
                    continue
                return self._parse(response, "GET", path)
            except ContextGraphError as exc:
                if exc.retryable and attempt < self.retries:
                    time.sleep(_retry_delay(response, attempt))
                    continue
                raise
            except httpx.HTTPError as exc:
                if attempt < self.retries:
                    time.sleep(_retry_delay(None, attempt))
                    continue
                raise ContextGraphError(ContextGraphErrorInfo(
                    message=f"Could not reach ContextGraph API ({type(exc).__name__})",
                    method="GET", path=path, retryable=True)) from exc
        raise AssertionError("retry loop exited unexpectedly")

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Perform one non-retried write request; registration is an upsert."""
        try:
            response = self._ensure_client().post(f"{self.base_url}{path}", json=payload, headers=self._headers)
            return self._parse(response, "POST", path)
        except ContextGraphError:
            raise
        except httpx.HTTPError as exc:
            raise ContextGraphError(ContextGraphErrorInfo(
                message=f"Could not reach ContextGraph API ({type(exc).__name__})",
                method="POST", path=path, retryable=False)) from exc


class AsyncTransport(_BaseTransport):
    def __init__(self, url: str, timeout: float = 15.0, retries: int = 1, api_key: str | None = None,
                 http_client: httpx.AsyncClient | None = None) -> None:
        super().__init__(url, timeout, retries, api_key)
        self._client: httpx.AsyncClient | None = http_client or httpx.AsyncClient(timeout=timeout, follow_redirects=False)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout, follow_redirects=False)
        return self._client

    async def get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        client = self._ensure_client()
        url = f"{self.base_url}{path}"
        for attempt in range(self.retries + 1):
            response = None
            try:
                response = await client.get(url, params=params, headers=self._headers)
                if response.status_code in _RETRYABLE_STATUS and attempt < self.retries:
                    await asyncio.sleep(_retry_delay(response, attempt))
                    continue
                return self._parse(response, "GET", path)
            except ContextGraphError as exc:
                if exc.retryable and attempt < self.retries:
                    await asyncio.sleep(_retry_delay(response, attempt))
                    continue
                raise
            except httpx.HTTPError as exc:
                if attempt < self.retries:
                    await asyncio.sleep(_retry_delay(None, attempt))
                    continue
                raise ContextGraphError(ContextGraphErrorInfo(
                    message=f"Could not reach ContextGraph API ({type(exc).__name__})",
                    method="GET", path=path, retryable=True)) from exc
        raise AssertionError("retry loop exited unexpectedly")

    async def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Perform one non-retried write request; registration is an upsert."""
        try:
            response = await self._ensure_client().post(f"{self.base_url}{path}", json=payload, headers=self._headers)
            return self._parse(response, "POST", path)
        except ContextGraphError:
            raise
        except httpx.HTTPError as exc:
            raise ContextGraphError(ContextGraphErrorInfo(
                message=f"Could not reach ContextGraph API ({type(exc).__name__})",
                method="POST", path=path, retryable=False)) from exc
