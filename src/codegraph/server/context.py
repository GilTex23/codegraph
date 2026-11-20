"""Select compact graph evidence for a task containing a symbol, route or path."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ..tokens import fit_lines

if TYPE_CHECKING:
    from .tools import GraphTools


_ANCHOR = re.compile(r"[\w./\\$:{\}-]+")
_HTTP = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(/[^\s`'\"]*)", re.I)
_IMPACT = re.compile(
    r"\b(calls?|callers?|uses?|callsites?|references?|affect\w*|impact|chang\w*|"
    r"renam\w*|delet\w*|вызов\w*|вызыв\w*|использу\w*|измен\w*|"
    r"переимен\w*|удал\w*|затрон\w*)\b",
    re.I,
)
_CALLEES = re.compile(r"\b(callees?|outgoing)\b|\bwhat does\b.+\bcall\b|что вызывает", re.I)
_RELATION = re.compile(r"\b(which|who|како\w*|кто)\b", re.I)
_FILLER = set(
    "who what where which the this that find show file function method for "
    "does implementation how are and где как что для найти покажи метод функция".split()
)


def task_context(tools: GraphTools, task: str, max_tokens: int) -> str:
    if max_tokens <= 0:
        raise ValueError("max_tokens must be positive")
    task = task.strip()
    if not task:
        raise ValueError("task must contain a symbol, route, path or search terms")
    return fit_lines(_evidence(tools, task), max_tokens)


def _evidence(tools: GraphTools, task: str) -> str:
    verb_route = _HTTP.search(task)
    if verb_route:
        return tools.trace_endpoint(f"{verb_route[1].upper()} {verb_route[2].rstrip('.,;')}")

    quoted = re.findall(r"`([^`]+)`", task)
    words = [s.strip(".:") for s in _ANCHOR.findall(task)]
    words = [s for s in words if s and s.lower() not in _FILLER]
    # Explicit anchors win over prose. Identifiers with punctuation precede generic words.
    ranked = sorted(words, key=lambda w: (w.isalpha() and w.islower(), -len(w)))
    anchors = dict.fromkeys([*quoted, task, *ranked[:16]])
    for anchor in anchors:
        if "/" in anchor or "\\" in anchor:
            path = anchor.replace("\\", "/").removeprefix("./")
            if tools._query("SELECT 1 FROM files WHERE path = ?", (path,)):
                return tools.get_file_outline(path)
            if path.startswith("/") and " " not in path:
                return tools.trace_endpoint(path)

        matches = tools._nodes_named(anchor)
        if not matches and "-" in anchor and " " not in anchor:
            # A route slug often appears in a ticket without its router prefix.
            endpoints = tools._query(
                "SELECT qualified_name FROM nodes WHERE type='endpoint' "
                "AND substr(name, -length(?)) = ? ORDER BY qualified_name LIMIT ?",
                ("/" + anchor, "/" + anchor, tools.limit),
            )
            if endpoints:
                return "\n".join(tools.trace_endpoint(r[0]) for r in endpoints)
        if len(matches) > 1:
            return tools._disambiguate(anchor, matches)
        if not matches:
            continue
        node = matches[0]
        if node.type == "module":
            if anchor in quoted or anchor == task:
                return tools.get_file_outline(node.path)
            continue
        _, warning = tools._checked_source(node.path, node.content_hash)
        if warning:
            return warning
        if node.type == "hook":
            return tools.get_neighbors(node.qualified_name)
        if node.type == "endpoint":
            return tools.trace_endpoint(node.qualified_name)
        label = node.qualified_name.partition("::")[2] or node.name
        header = f"{node.type} {label}  {node.path}:{node.line_start}"
        if node.type in ("class", "interface", "type_alias") and node.signature:
            header += "\n" + node.signature
        if _CALLEES.search(task):
            return header + "\n" + tools.get_callees(node.qualified_name)
        if _IMPACT.search(task) or _RELATION.search(task):
            callers = tools.get_callers(node.qualified_name).splitlines()
            confirmed = [s for s in callers if "[heuristic]" not in s]
            possible = [s for s in callers if "[heuristic]" in s]
            lines = [header, *confirmed]
            if possible:
                lines.extend(["Possible callers", *possible])
            return "\n".join(lines)
        routes = tools._query(
            "SELECT DISTINCT ep.qualified_name FROM edges e JOIN nodes ep ON "
            "(e.type='handles' AND ep.id=e.src_id AND e.dst_id=?) OR "
            "(e.type='calls_api' AND ep.id=e.dst_id AND e.src_id=?) "
            "WHERE ep.type='endpoint' ORDER BY ep.qualified_name LIMIT ?",
            (node.id, node.id, tools.limit),
        )
        if routes:
            return "\n".join(tools.trace_endpoint(r[0]) for r in routes)
        return header

    return tools.search_symbol(" ".join(words) or task, match_any=True)
