"""Deterministic, bounded lexical retrieval for developer-owned catalogs."""
from __future__ import annotations

import re
import math
from typing import Any

_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = {"a", "an", "and", "are", "as", "at", "by", "for", "from", "how", "i", "in", "is", "it", "of", "on", "or", "the", "to", "with", "this", "that", "find", "get", "check", "look", "up"}


def _tokens(value: str) -> set[str]:
    tokens = set()
    for token in _WORD.findall(value.lower()):
        if token.endswith("s") and len(token) > 4 and not token.endswith(("ss", "us", "is")):
            token = token[:-1]
        if token not in _STOPWORDS and len(token) > 1:
            tokens.add(token)
    return tokens


def retrieve_catalog_context(
    *, catalog: str, task: str, tools: list[dict[str, Any]], dependencies: list[tuple[str, str]],
    servers: list[tuple[str, str]], catalog_edges: list[tuple[str, str]] | None = None,
    max_tools: int = 8, known_tool_ids: list[str] | None = None,
    exclude_tool_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Rank and expand a real catalog snapshot; all edge closure is app-side and bounded."""
    if not 8 <= len(task) <= 1200:
        raise ValueError("task must contain 8–1200 characters")
    if not 1 <= max_tools <= 50:
        raise ValueError("max_tools must be between 1 and 50")
    by_id = {str(row["key"]): row for row in tools}
    known = set(known_tool_ids or [])
    excluded = set(exclude_tool_ids or [])
    unknown = (known | excluded) - set(by_id)
    if unknown:
        raise ValueError("known or excluded tools must exist in the selected catalog: " + ", ".join(sorted(unknown)[:5]))

    query_terms = _tokens(task)
    searchable = {ident: _tokens(f"{row.get('name', '')} {row.get('description') or ''}")
        for ident, row in by_id.items()}
    document_frequency = {term: sum(term in words for words in searchable.values()) for term in query_terms}
    term_weight = {term: math.log((len(searchable) + 1) / (document_frequency[term] + 1)) + 1
        for term in query_terms}
    ranked: list[tuple[float, str, list[str]]] = []
    for ident, row in by_id.items():
        if ident in known or ident in excluded:
            continue
        name_terms = _tokens(f"{row.get('name', '')} {ident}")
        description_terms = _tokens(str(row.get("description") or ""))
        name_matches = sorted(query_terms & name_terms)
        description_matches = sorted(query_terms & description_terms)
        # Exact metadata token overlap only, weighted by catalog rarity. This
        # stays deterministic and lexical; it is not semantic/vector ranking.
        score = 2.0 * sum(term_weight[term] for term in name_matches) + .75 * sum(
            term_weight[term] for term in description_matches)
        if score:
            reasons = []
            if name_matches:
                reasons.append("Task terms match tool name/ID: " + ", ".join(name_matches))
            if description_matches:
                reasons.append("Task terms match tool description: " + ", ".join(description_matches))
            ranked.append((score, ident, reasons))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    adjacency: dict[str, list[str]] = {}
    for source, target in dependencies:
        adjacency.setdefault(source, []).append(target)
    for source in adjacency:
        adjacency[source] = sorted(set(adjacency[source]))

    reasons_by_id = {ident: reasons for _, ident, reasons in ranked}
    selected: list[str] = []
    missing: set[str] = set()
    omitted: set[str] = set()
    expanded: set[str] = set()
    if ranked:
        threshold = max(1.0, ranked[0][0] * .65)
        ranked = [entry for entry in ranked if entry[0] >= threshold]
    omitted.update(ident for _, ident, _ in ranked[max_tools:])
    roots = [ident for _, ident, _ in ranked[:max_tools]]
    # Include explicitly known tools as context, but only after validating that
    # each ID exists in this exact catalog.
    for root in roots:
        if root not in selected:
            if len(selected) >= max_tools:
                omitted.add(root)
                continue
            selected.append(root)
        queue = [root]
        depth_by_id = {root: 0}
        while queue:
            current = queue.pop(0)
            if current in expanded:
                continue
            expanded.add(current)
            for prerequisite in adjacency.get(current, []):
                if prerequisite in known:
                    # Known context is a separate node category. It satisfies
                    # the dependency without consuming the retrieved-tool
                    # budget or being counted as a selected tool.
                    continue
                if prerequisite in excluded or prerequisite not in by_id:
                    missing.add(prerequisite)
                    continue
                if prerequisite not in selected:
                    if len(selected) >= max_tools:
                        missing.add(prerequisite)
                        continue
                    selected.append(prerequisite)
                    reasons_by_id[prerequisite] = [f"Required prerequisite of {current}"]
                if depth_by_id[current] < 8:
                    depth_by_id.setdefault(prerequisite, depth_by_id[current] + 1)
                    queue.append(prerequisite)

    selected_set = set(selected)
    graph_edges: list[dict[str, str]] = []
    edge_limit = 350
    edge_truncated = False
    for source, target in dependencies:
        if source in selected_set and (target in selected_set or target in known or target in missing):
            if target in selected_set or target in known:
                if len(graph_edges) < 250:
                    graph_edges.append({"from": source, "to": target, "type": "DEPENDS_ON"})
                else:
                    missing.add(target)
                    edge_truncated = True
    server_map = {tool: server for tool, server in servers}
    server_nodes: dict[str, dict[str, Any]] = {}
    catalog_node = {"id": f"catalog:{catalog}", "label": catalog, "type": "Catalog",
        "metadata": {"catalog": catalog}}
    nodes: list[dict[str, Any]] = [catalog_node]
    for ident in selected + sorted(known):
        row = by_id[ident]
        declared = adjacency.get(ident, [])
        metadata = {
            "name": row.get("name") or ident,
            "description": row.get("description") or "",
            "input_schema": row.get("input_schema") or row.get("schema"),
            "output_schema": row.get("output_schema"),
            "server": row.get("server"),
            "catalog": catalog,
            "dependencies": declared,
            "why": reasons_by_id.get(ident, []),
            "execution_provenance": row.get("provenance") or "developer_registered",
            "known_context": ident in known,
        }
        nodes.append({"id": ident, "label": row.get("name") or ident,
            "type": "KnownTool" if ident in known else "Tool", "metadata": metadata})
        if (ident, catalog) in (catalog_edges or []) and len(graph_edges) < edge_limit:
            graph_edges.append({"from": ident, "to": f"catalog:{catalog}", "type": "IN_CATALOG"})
        elif (ident, catalog) in (catalog_edges or []):
            edge_truncated = True
        server = server_map.get(ident)
        if server:
            server_id = f"server:{server}"
            if server_id not in server_nodes:
                server_nodes[server_id] = {"id": server_id, "label": server, "type": "MCPServer",
                    "metadata": {"name": server, "catalog": catalog}}
            if len(graph_edges) < edge_limit:
                graph_edges.append({"from": ident, "to": server_id, "type": "PROVIDED_BY"})
            else:
                edge_truncated = True
    nodes.extend(server_nodes.values())
    selected_ids = set(selected)
    missing.update(target for source, target in dependencies if source in selected_ids and target not in by_id and target not in known)
    return {
        "task": task, "catalog": catalog, "skills": [], "nodes": nodes, "edges": graph_edges,
        "resolved_entities": [], "retrieval_ms": None,
        "bounded": {"max_tools": max_tools, "selected_tools": len(selected), "max_edges": edge_limit,
            "returned_edges": len(graph_edges), "edge_truncated": edge_truncated, "traversal_depth": 8},
        "known_tool_ids": sorted(known), "missing_dependencies": sorted(missing),
        "omitted_tool_ids": sorted(omitted), "complete": not missing and not omitted,
    }
