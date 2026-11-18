"""Small deterministic retrieval evaluator; it never executes tools or calls a model."""
from __future__ import annotations

import json
import math
import re
import time
from typing import Any

from .models import EvaluationReport, EvaluationStrategyResult, EvaluationTaskResult, ToolDefinition

_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"a", "an", "and", "are", "as", "at", "by", "for", "from", "how", "i", "in", "is", "it", "of", "on", "or", "the", "to", "with", "this", "that", "find", "get", "check", "look", "up"}


def _tokens(text: str) -> set[str]:
    tokens = set()
    for token in _WORD.findall(text.lower()):
        if token.endswith("s") and len(token) > 4 and not token.endswith(("ss", "us", "is")):
            token = token[:-1]
        if token not in _STOP and len(token) > 1:
            tokens.add(token)
    return tokens


def _flat_select(task: str, tools: list[ToolDefinition], limit: int) -> tuple[list[ToolDefinition], float]:
    started = time.perf_counter()
    query = _tokens(task)
    ranked = []
    for tool in tools:
        name_terms = _tokens(f"{tool.id} {tool.name}")
        description_terms = _tokens(tool.description)
        score = 2 * len(query & name_terms) + len(query & description_terms)
        if score:
            ranked.append((score, tool.id, tool))
    ranked.sort(key=lambda row: (-row[0], row[1]))
    elapsed = (time.perf_counter() - started) * 1000
    return [row[2] for row in ranked[:limit]], elapsed


def _closure(required: set[str], by_id: dict[str, ToolDefinition]) -> set[str]:
    seen = set(required)
    stack = list(required)
    while stack:
        current = stack.pop()
        tool = by_id.get(current)
        if not tool:
            continue
        for prerequisite in tool.dependencies:
            if prerequisite not in seen:
                seen.add(prerequisite)
                stack.append(prerequisite)
    return seen


def _tokens_estimate(tools: list[ToolDefinition]) -> int:
    text = json.dumps([{"name": tool.name, "description": tool.description,
        "input_schema": tool.input_schema, "output_schema": tool.output_schema} for tool in tools],
        sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return math.ceil(len(text) / 4)


def _strategy_result(selected: list[ToolDefinition], required: set[str], expected: set[str],
                     retrieval_ms: float, missing: list[str] | None = None) -> EvaluationStrategyResult:
    selected_ids = {tool.id for tool in selected}
    recall = len(required & selected_ids) / len(required) if required else 1.0
    dependency_complete = not missing and all(
        dep in selected_ids for tool in selected for dep in tool.dependencies)
    return EvaluationStrategyResult(
        selected_tool_ids=[tool.id for tool in selected], required_tool_recall=recall,
        retrieved_tool_count=len(selected), dependency_complete=dependency_complete,
        unrelated_tool_count=len(selected_ids - expected),
        estimated_schema_tokens=_tokens_estimate(selected), retrieval_ms=round(retrieval_ms, 3))


def evaluate_results(catalog: str, task_specs: list[dict[str, Any]], tools: list[ToolDefinition],
                     graph_results: list[Any], max_tools: int) -> EvaluationReport:
    by_id = {tool.id: tool for tool in tools}
    rows: list[EvaluationTaskResult] = []
    for spec, graph in zip(task_specs, graph_results, strict=True):
        task = str(spec["task"])
        required = set(str(item) for item in spec["required_tools"])
        unknown = required - set(by_id)
        if unknown:
            raise ValueError(f"required tools are not in catalog {catalog!r}: {', '.join(sorted(unknown))}")
        expected = _closure(required, by_id)
        flat_tools, flat_ms = _flat_select(task, tools, max_tools)
        graph_row = _strategy_result(graph.tools, required, expected,
            graph.retrieval_ms or 0.0, graph.missing_dependencies)
        flat_row = _strategy_result(flat_tools, required, expected, flat_ms)
        rows.append(EvaluationTaskResult(task=task, required_tool_ids=sorted(required),
            graph=graph_row, flat=flat_row))

    def average(strategy: str, attribute: str) -> float:
        return round(sum(float(getattr(getattr(row, strategy), attribute)) for row in rows) / len(rows), 4)

    return EvaluationReport(catalog=catalog, tasks=rows,
        graph_average_required_tool_recall=average("graph", "required_tool_recall"),
        flat_average_required_tool_recall=average("flat", "required_tool_recall"),
        graph_average_retrieved_tools=average("graph", "retrieved_tool_count"),
        flat_average_retrieved_tools=average("flat", "retrieved_tool_count"),
        graph_dependency_complete_rate=average("graph", "dependency_complete"),
        flat_dependency_complete_rate=average("flat", "dependency_complete"),
        graph_average_unrelated_tools=average("graph", "unrelated_tool_count"),
        flat_average_unrelated_tools=average("flat", "unrelated_tool_count"),
        graph_average_estimated_schema_tokens=average("graph", "estimated_schema_tokens"),
        flat_average_estimated_schema_tokens=average("flat", "estimated_schema_tokens"))


def validate_task_specs(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("tasks must be a non-empty list")
    normalized = []
    for index, spec in enumerate(tasks):
        if not isinstance(spec, dict):
            raise ValueError(f"task {index} must be a mapping")
        task = spec.get("task")
        required = spec.get("required_tools")
        if not isinstance(task, str) or not 8 <= len(task) <= 1200:
            raise ValueError(f"task {index} must contain 8–1200 characters")
        if not isinstance(required, list) or not required or any(not isinstance(item, str) or not item for item in required):
            raise ValueError(f"task {index} must specify a non-empty required_tools list")
        if len(set(required)) != len(required):
            raise ValueError(f"task {index} contains duplicate required tool IDs")
        normalized.append({"task": task, "required_tools": list(required)})
    return normalized
