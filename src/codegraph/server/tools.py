"""The graph queries behind the MCP tools.

Kept separate from ``mcp_server`` so they can be tested (and used from the CLI)
without an MCP session.  Every function returns a compact string: the whole
point of this tool is spending fewer tokens than reading files would, so output
is dense, truncated to ``server.max_results``, and never includes function
bodies unless a tool explicitly promises them.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from ..config import Config
from ..db import Database

MAX_DEPTH = 2
NEIGHBOR_HARD_LIMIT = 60

_LAYER_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("model", ("models", "model", "entities", "domain")),
    ("schema", ("schemas", "schema", "dto", "serializers")),
    ("api", ("api", "routers", "routes", "endpoints", "views", "controllers")),
    ("service", ("services", "service", "crud", "repositories", "repository", "usecases")),
)

_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_CAMEL_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")


@dataclass(slots=True)
class _Row:
    id: int
    name: str
    type: str
    path: str
    line_start: int
    line_end: int
    signature: str | None
    docstring: str | None
    qualified_name: str


class GraphTools:
    """Read-only queries over a built graph."""

    def __init__(self, config: Config, db: Database | None = None) -> None:
        self.config = config
        self.db = db or Database.open_readonly(config.db_path)
        # Tool calls can arrive on different worker threads; SQLite access here
        # is read-only, so one lock is enough to keep it safe.
        self._lock = threading.RLock()

    def close(self) -> None:
        self.db.close()

    # --------------------------------------------------------------- helpers

    @property
    def limit(self) -> int:
        return self.config.server.max_results

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.db.conn.execute(sql, params).fetchall()

    def _nodes_named(self, name: str) -> list[_Row]:
        rows = self._query(
            "SELECT n.id, n.name, n.type, f.path, n.line_start, n.line_end, n.signature, "
            "n.docstring, n.qualified_name FROM nodes n JOIN files f ON f.id = n.file_id "
            "WHERE n.name = ? OR n.qualified_name = ? ORDER BY "
            "CASE n.type WHEN 'module' THEN 1 ELSE 0 END, f.path",
            (name, name),
        )
        if not rows:
            rows = self._query(
                "SELECT n.id, n.name, n.type, f.path, n.line_start, n.line_end, n.signature, "
                "n.docstring, n.qualified_name FROM nodes n JOIN files f ON f.id = n.file_id "
                "WHERE n.qualified_name LIKE ? ORDER BY f.path LIMIT 20",
                (f"%::{name}",),
            )
        return [_Row(**dict(row)) for row in rows]

    # -------------------------------------------------------------- searching

    def search_symbol(self, query: str, type: str | None = None) -> str:
        expression = _fts_expression(query)
        rows: list[sqlite3.Row] = []
        if expression:
            sql = (
                "SELECT n.id, n.name, n.type, f.path, n.line_start, n.signature "
                "FROM nodes_fts JOIN nodes n ON n.id = nodes_fts.rowid "
                "JOIN files f ON f.id = n.file_id WHERE nodes_fts MATCH ?"
            )
            params: list[object] = [expression]
            if type:
                sql += " AND n.type = ?"
                params.append(type)
            # Module nodes match almost any path-ish query; keep them last.
            sql += " ORDER BY (n.type = 'module'), rank LIMIT ?"
            params.append(self.limit + 1)
            try:
                rows = self._query(sql, tuple(params))
            except sqlite3.OperationalError:
                rows = []
        if not rows:
            sql = (
                "SELECT n.id, n.name, n.type, f.path, n.line_start, n.signature "
                "FROM nodes n JOIN files f ON f.id = n.file_id WHERE n.name LIKE ?"
            )
            params = [f"%{query}%"]
            if type:
                sql += " AND n.type = ?"
                params.append(type)
            sql += " ORDER BY length(n.name), n.name LIMIT ?"
            params.append(self.limit + 1)
            rows = self._query(sql, tuple(params))

        if not rows:
            return f"no symbol matches {query!r}"
        lines = [
            f"{row['name']}  {row['type']}  {row['path']}:{row['line_start']}"
            f"{'  ' + row['signature'] if row['signature'] else ''}"
            for row in rows[: self.limit]
        ]
        return _with_truncation_note(lines, len(rows), self.limit)

    # ------------------------------------------------------------ definitions

    def get_definition(self, name: str) -> str:
        matches = self._nodes_named(name)
        if not matches:
            return f"no definition found for {name!r}"
        if len(matches) > 1:
            header = f"{len(matches)} definitions named {name!r}; pass a qualified_name:"
            lines = [
                f"  {row.qualified_name}  ({row.type}, {row.path}:{row.line_start})"
                for row in matches[: self.limit]
            ]
            return "\n".join([header, *lines])

        row = matches[0]
        out = [
            f"{row.qualified_name}  [{row.type}]",
            f"{row.path}:{row.line_start}-{row.line_end}",
        ]
        if row.signature:
            out.append(row.signature)
        if row.docstring:
            out.append(f'"""{row.docstring}"""')

        span = row.line_end - row.line_start + 1
        budget = self.config.server.snippet_max_lines
        if span <= budget:
            source = self._read_lines(row.path, row.line_start, row.line_end)
            if source is not None:
                out.append("---")
                out.append(source)
        else:
            out.append(
                f"--- body omitted ({span} lines > snippet_max_lines={budget}); "
                f"read {row.path} lines {row.line_start}-{row.line_end}"
            )
        return "\n".join(out)

    def _read_lines(self, rel_path: str, start: int, end: int) -> str | None:
        path = self.config.root / rel_path
        try:
            with open(path, encoding="utf-8", errors="replace", newline="") as handle:
                text = handle.read()
        except OSError:
            return None
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        return "\n".join(lines[start - 1 : end])

    # ------------------------------------------------------------------ edges

    def get_callers(self, name: str) -> str:
        rows = self._query(
            "SELECT sf.path, s.name, s.type, e.line, e.confidence "
            "FROM edges e JOIN nodes s ON s.id = e.src_id JOIN files sf ON sf.id = s.file_id "
            "JOIN nodes t ON t.id = e.dst_id "
            "WHERE e.type = 'calls' AND t.name = ? AND e.resolved = 1 "
            "ORDER BY sf.path, e.line LIMIT ?",
            (name, self.limit + 1),
        )
        if not rows:
            unresolved = self._query(
                "SELECT count(*) FROM edges WHERE type = 'calls' AND dst_name = ? AND resolved = 0",
                (name,),
            )[0][0]
            if unresolved:
                return (
                    f"no resolved callers of {name!r}; "
                    f"{unresolved} unresolved call site(s) mention that name"
                )
            return f"no callers of {name!r}"
        lines = [
            f"{row['name']}  {row['type']}  {row['path']}:{row['line']}"
            f"{'  [heuristic]' if row['confidence'] == 'heuristic' else ''}"
            for row in rows[: self.limit]
        ]
        return _with_truncation_note(lines, len(rows), self.limit)

    def get_callees(self, name: str) -> str:
        matches = self._nodes_named(name)
        if not matches:
            return f"no definition found for {name!r}"
        ids = tuple(row.id for row in matches[:5])
        placeholders = ",".join("?" * len(ids))
        rows = self._query(
            "SELECT e.dst_name, e.line, e.resolved, e.confidence, tf.path, t.type "
            "FROM edges e LEFT JOIN nodes t ON t.id = e.dst_id "
            "LEFT JOIN files tf ON tf.id = t.file_id "
            f"WHERE e.type = 'calls' AND e.src_id IN ({placeholders}) "
            "ORDER BY e.line LIMIT ?",
            (*ids, self.limit + 1),
        )
        if not rows:
            return f"{name!r} calls nothing (or has no body in the graph)"
        lines = []
        for row in rows[: self.limit]:
            if row["resolved"]:
                mark = "  [heuristic]" if row["confidence"] == "heuristic" else ""
                lines.append(f"{row['dst_name']}  {row['type']}  {row['path']}:{row['line']}{mark}")
            elif row["confidence"] == "external":
                lines.append(f"{row['dst_name']}  third-party  (call at line {row['line']})")
            else:
                lines.append(f"{row['dst_name']}  ?  unresolved  (call at line {row['line']})")
        return _with_truncation_note(lines, len(rows), self.limit)

    # ------------------------------------------------------------------ files

    def get_file_outline(self, path: str) -> str:
        file = self._find_file(path)
        if file is None:
            return f"file not indexed: {path}"
        rows = self._query(
            "SELECT id, type, name, qualified_name, parent_id, line_start, line_end, signature, "
            "is_async, is_exported FROM nodes WHERE file_id = ? AND type != 'module' "
            "ORDER BY line_start",
            (file["id"],),
        )
        if not rows:
            return f"{file['path']}  ({file['language']}) - no declarations"
        depth: dict[int, int] = {}
        out = [f"{file['path']}  ({file['language']})"]
        for row in rows[: self.limit]:
            level = depth.get(row["parent_id"], -1) + 1 if row["parent_id"] else 0
            depth[row["id"]] = level
            indent = "  " * level
            label = row["signature"] or row["name"]
            flags = "" if row["is_exported"] else "  [not exported]"
            span = f"{row['line_start']}-{row['line_end']}"
            out.append(f"{indent}{span}  {row['type']}  {label}{flags}")
        if len(rows) > self.limit:
            out.append(f"... {len(rows) - self.limit} more declarations")
        return "\n".join(out)

    def get_imports(self, path: str) -> str:
        file = self._find_file(path)
        if file is None:
            return f"file not indexed: {path}"
        outgoing = self._query(
            "SELECT i.module, i.symbol, i.alias, i.line, i.is_reexport FROM imports i "
            "WHERE i.file_id = ? ORDER BY i.line LIMIT ?",
            (file["id"], self.limit + 1),
        )
        targets = self._query(
            "SELECT DISTINCT tf.path FROM edges e JOIN nodes s ON s.id = e.src_id "
            "JOIN nodes t ON t.id = e.dst_id JOIN files tf ON tf.id = t.file_id "
            "WHERE e.type = 'imports' AND s.file_id = ? ORDER BY tf.path",
            (file["id"],),
        )
        importers = self._query(
            "SELECT DISTINCT sf.path FROM edges e JOIN nodes s ON s.id = e.src_id "
            "JOIN files sf ON sf.id = s.file_id JOIN nodes t ON t.id = e.dst_id "
            "WHERE e.type = 'imports' AND t.file_id = ? ORDER BY sf.path LIMIT ?",
            (file["id"], self.limit + 1),
        )

        out = [f"{file['path']}  ({file['language']})", "imports:"]
        if outgoing:
            for row in outgoing[: self.limit]:
                what = row["symbol"] or "*module*"
                alias = f" as {row['alias']}" if row["alias"] else ""
                tag = "  [re-export]" if row["is_reexport"] else ""
                out.append(f"  {row['module']}  {what}{alias}  (line {row['line']}){tag}")
        else:
            out.append("  (none)")
        out.append("resolves to files:")
        if targets:
            out.extend(f"  {row['path']}" for row in targets)
        else:
            out.append("  (none)")
        out.append("imported by:")
        if importers:
            out.extend(f"  {row['path']}" for row in importers[: self.limit])
        else:
            out.append("  (nobody)")
        return "\n".join(out)

    def _find_file(self, path: str) -> sqlite3.Row | None:
        normalized = Path(path).as_posix().lstrip("./")
        row = self._query(
            "SELECT id, path, language FROM files WHERE path = ? OR lower(path) = ?",
            (normalized, normalized.lower()),
        )
        if row:
            return row[0]
        row = self._query(
            "SELECT id, path, language FROM files WHERE path LIKE ? ORDER BY length(path) LIMIT 1",
            (f"%{normalized}",),
        )
        return row[0] if row else None

    # ------------------------------------------------------------ directories

    def get_directory_outline(self, path: str) -> str:
        """One line per file: how many declarations and the exported names.

        Deliberately coarser than ``get_file_outline`` -- this answers "which
        file do I want", and seventeen full outlines would cost more than
        reading a file.
        """
        prefix = Path(path).as_posix().strip("/").lstrip("./")
        rows = self._query(
            "SELECT f.path, n.name, n.type, n.is_exported, n.line_start "
            "FROM files f LEFT JOIN nodes n ON n.file_id = f.id AND n.type != 'module' "
            "WHERE f.path = ? OR f.path LIKE ? ORDER BY f.path, n.line_start",
            (prefix, f"{prefix}/%"),
        )
        if not rows:
            return f"no indexed files under {path!r}"

        files: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            files.setdefault(row["path"], [])
            if row["name"]:
                files[row["path"]].append(row)

        out = [f"{prefix}/  {len(files)} files"]
        for file_path, declarations in list(files.items())[: self.limit]:
            # Exports first, then internals to fill the line: a page module
            # exports one component whose name repeats the filename, which tells
            # the reader nothing they did not already have. Constants sink to
            # the bottom -- a module opening with a block of UPPER_CASE would
            # otherwise spend the whole line saying nothing about what it does.
            ranked = sorted(
                declarations,
                key=lambda row: (row["type"] == "variable", not row["is_exported"]),
            )
            headline = list(dict.fromkeys(row["name"] for row in ranked))
            shown = ", ".join(headline[:6])
            more = "…" if len(headline) > 6 else ""
            name = file_path[len(prefix) + 1 :] if file_path != prefix else file_path
            out.append(f"  {name}  {len(declarations)}  {shown}{more}")
        if len(files) > self.limit:
            out.append(f"... {len(files) - self.limit} more files")
        return "\n".join(out)

    # ----------------------------------------------------------- change impact

    def get_change_impact(self, base: str | None = None) -> str:
        """Declarations you are currently editing, plus who calls them."""
        changes, source = self._changed_regions(base)
        if not changes:
            return f"no changes detected ({source})"

        out = [f"changed files ({source}):"]
        budget = self.limit
        for rel_path, ranges in list(changes.items())[: self.limit]:
            file = self._find_file(rel_path)
            if file is None:
                out.append(f"{rel_path}  (not indexed)")
                continue
            where = _render_ranges(ranges)
            out.append(f"{file['path']}{'  ' + where if where else ''}")
            for node in self._nodes_touching(file["id"], ranges)[:budget]:
                label = node["signature"] or node["name"]
                if len(label) > 90:  # a changed constant should not spend a line on its value
                    label = label[:89] + "…"
                out.append(f"  {node['type']} {label}  L{node['line_start']}-{node['line_end']}")
                callers = self._query(
                    "SELECT s.name, sf.path, e.line, e.confidence FROM edges e "
                    "JOIN nodes s ON s.id = e.src_id JOIN files sf ON sf.id = s.file_id "
                    "WHERE e.type IN ('calls', 'calls_api') AND e.dst_id = ? "
                    "ORDER BY sf.path LIMIT 6",
                    (node["id"],),
                )
                for caller in callers:
                    mark = " [heuristic]" if caller["confidence"] == "heuristic" else ""
                    out.append(f"    <- {caller['name']}  {caller['path']}:{caller['line']}{mark}")
                if not callers:
                    out.append("    <- no callers in the graph")
        if len(changes) > self.limit:
            out.append(f"... {len(changes) - self.limit} more files")
        return "\n".join(out)

    def _nodes_touching(self, file_id: int, ranges: list[tuple[int, int]]) -> list[sqlite3.Row]:
        """Declarations overlapping the changed lines; all of them if unknown."""
        if not ranges:
            return self._query(
                "SELECT id, name, type, signature, line_start, line_end FROM nodes "
                "WHERE file_id = ? AND type != 'module' AND parent_id IS NULL "
                "ORDER BY line_start LIMIT 20",
                (file_id,),
            )
        seen: dict[int, sqlite3.Row] = {}
        for start, end in ranges:
            for row in self._query(
                "SELECT id, name, type, signature, line_start, line_end FROM nodes "
                "WHERE file_id = ? AND type != 'module' AND line_start <= ? AND line_end >= ? "
                "ORDER BY (line_end - line_start) LIMIT 5",
                (file_id, end, start),
            ):
                seen.setdefault(row["id"], row)
        return sorted(seen.values(), key=lambda row: row["line_start"])

    def _changed_regions(self, base: str | None) -> tuple[dict[str, list[tuple[int, int]]], str]:
        """Which files changed, and where -- from git, or from the graph itself.

        Git is the better answer but not a given: the project may not be a
        repository, git may not be installed, or the tool may run somewhere it
        cannot execute it. Falling back to hashes the graph already stores keeps
        the tool useful instead of turning a missing binary into an error.
        """
        from_git = _git_changes(self.config.root, base)
        if from_git is not None:
            return from_git, "git working tree" if base is None else f"git, against {base}"

        stale = self._files_differing_from_the_graph()
        return stale, "no git here; files changed since the last build"

    def _files_differing_from_the_graph(self) -> dict[str, list[tuple[int, int]]]:
        from ..indexer.walker import hash_text, read_source

        changed: dict[str, list[tuple[int, int]]] = {}
        for row in self._query("SELECT path, content_hash FROM files ORDER BY path"):
            try:
                current = hash_text(read_source(self.config.root / row["path"]))
            except OSError:
                continue
            if current != row["content_hash"]:
                changed[row["path"]] = []
        return changed

    # -------------------------------------------------------------- neighbors

    def get_neighbors(self, name: str, depth: int = 1) -> str:
        depth = max(1, min(depth, MAX_DEPTH))
        matches = self._nodes_named(name)
        if not matches:
            return f"no definition found for {name!r}"
        start = matches[0]

        seen: set[int] = {start.id}
        frontier = [start.id]
        lines: list[str] = [
            f"{start.qualified_name}  [{start.type}]  {start.path}:{start.line_start}"
        ]
        for level in range(depth):
            if not frontier or len(lines) > NEIGHBOR_HARD_LIMIT:
                break
            placeholders = ",".join("?" * len(frontier))
            rows = self._query(
                "SELECT e.type, e.confidence, 'out' AS direction, t.id, t.name, t.type AS ntype, "
                "tf.path, t.line_start FROM edges e JOIN nodes t ON t.id = e.dst_id "
                f"JOIN files tf ON tf.id = t.file_id WHERE e.src_id IN ({placeholders}) "
                "UNION ALL "
                "SELECT e.type, e.confidence, 'in' AS direction, s.id, s.name, s.type AS ntype, "
                "sf.path, s.line_start FROM edges e JOIN nodes s ON s.id = e.src_id "
                f"JOIN files sf ON sf.id = s.file_id WHERE e.dst_id IN ({placeholders}) "
                "LIMIT ?",
                (*frontier, *frontier, NEIGHBOR_HARD_LIMIT),
            )
            frontier = []
            for row in rows:
                if row["id"] in seen or len(lines) > NEIGHBOR_HARD_LIMIT:
                    continue
                seen.add(row["id"])
                frontier.append(row["id"])
                arrow = "->" if row["direction"] == "out" else "<-"
                mark = " [heuristic]" if row["confidence"] == "heuristic" else ""
                lines.append(
                    f"{'  ' * (level + 1)}{arrow} {row['type']} {row['name']}  {row['ntype']}  "
                    f"{row['path']}:{row['line_start']}{mark}"
                )
        if len(lines) > NEIGHBOR_HARD_LIMIT:
            lines = lines[:NEIGHBOR_HARD_LIMIT]
            lines.append("... truncated")
        return "\n".join(lines)

    # --------------------------------------------------------------- overview

    def get_project_overview(self) -> str:
        with self._lock:
            counts = self.db.counts()
        rows = self._query(
            "SELECT f.path, n.name, n.type, n.is_exported FROM files f "
            "LEFT JOIN nodes n ON n.file_id = f.id AND n.type != 'module' ORDER BY f.path"
        )
        directories: dict[str, dict[str, object]] = {}
        for row in rows:
            directory = row["path"].rsplit("/", 1)[0] if "/" in row["path"] else "."
            entry = directories.setdefault(directory, {"files": set(), "symbols": []})
            entry["files"].add(row["path"])  # type: ignore[union-attr]
            if (
                row["name"]
                and row["is_exported"]
                and row["type"] in ("class", "function", "component", "interface", "endpoint")
            ):
                entry["symbols"].append(row["name"])  # type: ignore[union-attr]

        out = [
            f"{counts['files']} files, {counts['nodes']} nodes, {counts['edges']} edges "
            f"({counts['unresolved_edges']} unresolved)"
        ]
        for directory in sorted(directories)[: self.limit]:
            entry = directories[directory]
            symbols = list(dict.fromkeys(entry["symbols"]))[:8]  # type: ignore[index]
            suffix = f"  {', '.join(symbols)}" if symbols else ""
            out.append(f"{directory}/  {len(entry['files'])} files{suffix}")  # type: ignore[arg-type]
        if len(directories) > self.limit:
            out.append(f"... {len(directories) - self.limit} more directories")
        return "\n".join(out)

    # ------------------------------------------------------------ domain slice

    def get_domain_slice(self, domain: str) -> str:
        wanted = _normalize_domain(domain)
        if not wanted:
            return "empty domain name"

        files = self._query("SELECT id, path, language FROM files ORDER BY path")
        by_layer: dict[str, list[tuple[str, list[str]]]] = {}
        for file in files:
            stem = file["path"].rsplit("/", 1)[-1].rsplit(".", 1)[0]
            if _normalize_domain(stem) != wanted:
                continue
            symbols = self._query(
                "SELECT name, type, signature, line_start FROM nodes WHERE file_id = ? "
                "AND type NOT IN ('module', 'variable') ORDER BY line_start LIMIT 12",
                (file["id"],),
            )
            entries = [f"{row['line_start']}: {row['signature'] or row['name']}" for row in symbols]
            by_layer.setdefault(_layer_of(file["path"], file["language"]), []).append(
                (file["path"], entries)
            )

        # Also pick up symbols whose own name matches, in files that do not.
        loose = self._query(
            "SELECT n.name, n.type, f.path, n.line_start FROM nodes n JOIN files f "
            "ON f.id = n.file_id WHERE n.type IN ('class','interface','component','function') "
            "ORDER BY f.path LIMIT 4000"
        )
        extra = [
            f"{row['name']}  {row['type']}  {row['path']}:{row['line_start']}"
            for row in loose
            if _normalize_domain(row["name"]) == wanted
        ]

        if not by_layer and not extra:
            return f"nothing matches domain {domain!r}"

        out = [f"domain slice: {domain}"]
        for layer in ("model", "schema", "api", "service", "frontend", "other"):
            group = by_layer.get(layer)
            if not group:
                continue
            out.append(f"[{layer}]")
            for path, entries in group[: self.limit]:
                out.append(f"  {path}")
                out.extend(f"    {entry}" for entry in entries)
        if extra:
            out.append("[symbols elsewhere]")
            out.extend(f"  {item}" for item in list(dict.fromkeys(extra))[: self.limit])
        return "\n".join(out)

    # ------------------------------------------------------------- endpoints

    def trace_endpoint(self, path_or_name: str) -> str:
        # Match on the route path, or on the name of the function handling it.
        endpoints = self._query(
            "SELECT DISTINCT n.id, n.qualified_name, n.name, f.path, n.line_start FROM nodes n "
            "JOIN files f ON f.id = n.file_id "
            "LEFT JOIN edges h ON h.src_id = n.id AND h.type = 'handles' "
            "LEFT JOIN nodes handler ON handler.id = h.dst_id "
            "WHERE n.type = 'endpoint' AND (n.qualified_name LIKE ? OR n.name LIKE ? "
            "OR handler.name = ? OR handler.qualified_name = ?) "
            "ORDER BY n.qualified_name LIMIT ?",
            (f"%{path_or_name}%", f"%{path_or_name}%", path_or_name, path_or_name, self.limit),
        )
        if not endpoints:
            return self._trace_without_bridge(path_or_name)

        out: list[str] = []
        for endpoint in endpoints:
            where = f"{endpoint['path']}:{endpoint['line_start']}"
            out.append(f"{endpoint['qualified_name']}  ({where})")
            handler = self._query(
                "SELECT t.id, t.name, t.signature, tf.path, t.line_start FROM edges e "
                "JOIN nodes t ON t.id = e.dst_id JOIN files tf ON tf.id = t.file_id "
                "WHERE e.type = 'handles' AND e.src_id = ?",
                (endpoint["id"],),
            )
            if not handler:
                out.append("  handler: (not resolved)")
                continue
            handler_row = handler[0]
            out.append(
                f"  handler: {handler_row['name']}  {handler_row['path']}:"
                f"{handler_row['line_start']}  {handler_row['signature'] or ''}".rstrip()
            )
            for row in self._query(
                "SELECT e.dst_name, t.type, tf.path, t.line_start, e.confidence FROM edges e "
                "LEFT JOIN nodes t ON t.id = e.dst_id LEFT JOIN files tf ON tf.id = t.file_id "
                "WHERE e.type = 'calls' AND e.src_id = ? AND e.resolved = 1 LIMIT ?",
                (handler_row["id"], self.limit),
            ):
                kind = "model" if row["type"] == "class" else (row["type"] or "?")
                mark = " [heuristic]" if row["confidence"] == "heuristic" else ""
                where = f"{row['path']}:{row['line_start']}"
                out.append(f"  -> {kind} {row['dst_name']}  {where}{mark}")
            for row in self._query(
                "SELECT s.name, sf.path, e.line, e.confidence FROM edges e "
                "JOIN nodes s ON s.id = e.src_id JOIN files sf ON sf.id = s.file_id "
                "WHERE e.type = 'calls_api' AND e.dst_id = ? LIMIT ?",
                (endpoint["id"], self.limit),
            ):
                mark = " [heuristic]" if row["confidence"] == "heuristic" else ""
                out.append(f"  <- frontend {row['name']}  {row['path']}:{row['line']}{mark}")
        return "\n".join(out)

    def _trace_without_bridge(self, path_or_name: str) -> str:
        has_endpoints = self._query("SELECT count(*) FROM nodes WHERE type = 'endpoint'")[0][0]
        note = (
            "note: HTTP bridge is off or found no routes; backend-only view\n"
            if not has_endpoints
            else ""
        )
        matches = self._nodes_named(path_or_name)
        if not matches:
            return f"{note}no endpoint or function matches {path_or_name!r}".strip()
        row = matches[0]
        body = [f"{row.qualified_name}  [{row.type}]  {row.path}:{row.line_start}"]
        body.append(self.get_callees(row.qualified_name))
        return note + "\n".join(body)


# ------------------------------------------------------------------ utilities


_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _git(root: Path, *args: str) -> str | None:
    """Run a read-only git command; ``None`` if git cannot answer."""
    try:
        finished = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None  # git missing, unrunnable, or too slow
    return finished.stdout if finished.returncode == 0 else None


def _git_changes(root: Path, base: str | None) -> dict[str, list[tuple[int, int]]] | None:
    """Changed files and their new line ranges, or ``None`` without git."""
    if _git(root, "rev-parse", "--is-inside-work-tree") is None:
        return None

    arguments = ["diff", "--unified=0", "--no-color", "--no-ext-diff"]
    if base:
        arguments.append(base)
    diff = _git(root, *arguments)
    if diff is None:
        return None

    changes: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            current = None if target == "/dev/null" else target.removeprefix("b/")
            if current:
                changes.setdefault(current, [])
        elif current and (match := _HUNK_RE.match(line)):
            start = int(match.group(1))
            count = int(match.group(2) or 1)
            if count:
                changes[current].append((start, start + count - 1))

    # Brand new files have no hunks to parse but are very much part of the work.
    untracked = _git(root, "ls-files", "--others", "--exclude-standard")
    for name in (untracked or "").splitlines():
        if name.strip():
            changes.setdefault(name.strip(), [])
    return changes


def _render_ranges(ranges: list[tuple[int, int]]) -> str:
    if not ranges:
        return ""
    parts = [str(start) if start == end else f"{start}-{end}" for start, end in ranges[:6]]
    more = "…" if len(ranges) > 6 else ""
    return f"lines {', '.join(parts)}{more}"


def _with_truncation_note(lines: list[str], total: int, limit: int) -> str:
    if total > limit:
        lines = [*lines, f"... more results truncated (limit {limit})"]
    return "\n".join(lines)


def split_identifier(text: str) -> list[str]:
    """``fetchTaskList`` / ``fetch_task_list`` -> ['fetch', 'task', 'list']."""
    parts: list[str] = []
    for chunk in _WORD_RE.findall(text):
        parts.extend(match.group(0).lower() for match in _CAMEL_RE.finditer(chunk))
    return [part for part in parts if part]


def _fts_expression(query: str) -> str:
    """Build an FTS5 expression that also matches camelCase identifiers.

    ``parse`` has to find ``parseConfig``, which the tokenizer keeps whole, so
    every term is turned into a prefix query.
    """
    terms = _WORD_RE.findall(query)
    if not terms:
        return ""
    clauses: list[str] = []
    for term in terms[:8]:
        pieces = split_identifier(term)
        options = {f'"{term.lower()}"*'}
        if len(pieces) > 1:
            options.add(" AND ".join(f'"{piece}"*' for piece in pieces))
        clauses.append("(" + " OR ".join(sorted(options)) + ")")
    return " AND ".join(clauses)


def _singular(word: str) -> str:
    if len(word) > 3 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith(("ses", "xes", "zes", "ches", "shes")):
        return word[:-2]
    if len(word) > 2 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _normalize_domain(text: str) -> str:
    """Collapse naming styles and plurals so one domain has one key.

    ``DesignRequests``, ``design_requests`` and ``designRequest`` all become
    ``designrequest``.
    """
    words = split_identifier(text)
    if not words:
        return ""
    words = [*words[:-1], _singular(words[-1])]
    return "".join(words)


def _layer_of(path: str, language: str) -> str:
    parts = set(path.lower().split("/"))
    if language in ("typescript", "tsx"):
        return "frontend"
    for layer, markers in _LAYER_RULES:
        if parts & set(markers):
            return layer
    stem = path.rsplit("/", 1)[-1].lower()
    for layer, markers in _LAYER_RULES:
        if any(marker.rstrip("s") in stem for marker in markers):
            return layer
    return "other"
