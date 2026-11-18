"""Public response models for the ContextGraph API."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class HealthResponse(BaseModel):
    ok: bool
    hydradb: str
    probe_ms: float | None = None


class ToolDefinition(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    description: str = ""
    input_schema: dict[str, Any] | str | None = None
    output_schema: dict[str, Any] | str | None = None
    server: str | None = None
    skill: str | None = None
    dependencies: list[str] = Field(default_factory=list)
    provenance: str | None = None
    execution_status: str | None = None

    @field_validator("dependencies", mode="before")
    @classmethod
    def normalize_dependencies(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
            raise ValueError("dependencies must be a list of tool IDs")
        return list(value)


class ToolDependency(BaseModel):
    source_tool_id: str
    required_tool_id: str
    relationship: str = "DEPENDS_ON"


class RetrievalMetadata(BaseModel):
    skills: list[dict[str, Any]] = Field(default_factory=list)
    resolved_entities: list[dict[str, Any]] = Field(default_factory=list)
    bounded: dict[str, Any] = Field(default_factory=dict)
    endpoint: str


class RetrievalResult(BaseModel):
    task: str
    tools: list[ToolDefinition]
    dependencies: list[ToolDependency]
    selection_reasons: dict[str, list[str]]
    missing_dependencies: list[str] = Field(default_factory=list)
    retrieval_ms: float | None = None
    source: str
    execution_performed: Literal[False] = False
    metadata: RetrievalMetadata


class RetrievalExplanation(BaseModel):
    task: str
    selected_tool_ids: list[str]
    selection_reasons: dict[str, list[str]]
    dependencies: list[ToolDependency]
    missing_dependencies: list[str]
    source: str
    retrieval_ms: float | None = None
    execution_performed: Literal[False] = False


class ContextGraphErrorInfo(BaseModel):
    message: str
    status_code: int | None = None
    method: str | None = None
    path: str | None = None
    retryable: bool = False
