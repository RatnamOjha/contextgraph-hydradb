"""Typed exceptions raised by the ContextGraph SDK."""
from __future__ import annotations

from .models import ContextGraphErrorInfo


class ContextGraphError(RuntimeError):
    """An API, transport, or response-validation error with safe context."""

    def __init__(self, info: ContextGraphErrorInfo) -> None:
        self.info = info
        super().__init__(info.message)

    @classmethod
    def from_response(cls, message: str, path: str) -> "ContextGraphError":
        return cls(ContextGraphErrorInfo(message=message, method="GET", path=path))

    @property
    def status_code(self) -> int | None:
        return self.info.status_code

    @property
    def retryable(self) -> bool:
        return self.info.retryable

    def __repr__(self) -> str:
        # Deliberately omit request headers and credentials.
        return f"ContextGraphError({self.info.message!r})"
