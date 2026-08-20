"""Turning raw target names into real graph edges.

Runs as one whole-project pass after every file has been parsed, because a
change in one file can change how names resolve everywhere else.  Every edge's
``dst_id``/``resolved``/``confidence`` is recomputed from scratch each run.

Resolution order for a name used in file F (highest priority first):

1. a declaration in F itself                      -> resolved, exact
2. a name F explicitly imports                    -> resolved, exact
3. the only declaration with that name project-wide -> resolved, heuristic
4. nothing                                        -> unresolved, ``dst_name`` kept

Step 2 follows **re-export chains**.  ``from app.models import Task`` normally
lands on ``app/models/__init__.py``; if that file merely re-exports ``Task``
from ``app/models/task.py``, the chain is followed to the real declaration.
Without this, a large share of edges would pile up on aggregator files and the
graph would be close to useless.  The same applies to TS barrels
(``export * from './x'``, ``export { A } from './x'``).
"""

from __future__ import annotations

import builtins
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..db import Database

MAX_REEXPORT_DEPTH = 4

# Edge kinds whose dst_name is a symbol; 'imports' is handled separately.
SYMBOL_EDGE_TYPES = ("calls", "inherits", "decorates", "implements", "references")

TS_EXTENSIONS = (".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mjs", ".cjs")
PY_EXTENSIONS = (".py", ".pyi")
PHP_EXTENSIONS = (".php",)

# Bare-name calls that carry no information for an agent.  Python builtins plus
# the handful of JS/TS globals that show up constantly.  A project that defines
# one of these names itself keeps its edges -- see _drop_builtin_calls.
_JS_GLOBALS = frozenset(
    """
    Array BigInt Boolean Date Error EvalError Function Infinity Intl JSON Map Math NaN Number
    Object Promise Proxy RangeError ReferenceError Reflect RegExp Set String Symbol SyntaxError
    TypeError URIError WeakMap WeakSet clearInterval clearTimeout console decodeURI
    decodeURIComponent encodeURI encodeURIComponent isFinite isNaN parseFloat parseInt
    queueMicrotask require setInterval setTimeout structuredClone
    """.split()
)

# PHP's standard library is procedural and unimported, so its noisiest names
# have to be listed rather than derived. Only the ones that appear constantly
# and say nothing about a codebase.
_PHP_BUILTINS = frozenset(
    """
    abs array_column array_diff array_fill array_filter array_flip array_key_exists array_keys
    array_map array_merge array_pop array_push array_reverse array_search array_shift array_slice
    array_splice array_sum array_unique array_unshift array_values arsort asort basename
    call_user_func call_user_func_array ceil class_exists compact count current date defined
    define dirname empty
    end explode file_exists file_get_contents file_put_contents filemtime filter_var floatval floor
    function_exists func_get_args gettype implode in_array intdiv intval is_array is_bool
    is_callable is_dir is_file is_float is_int is_null is_numeric is_object is_string isset
    iterator_to_array json_decode json_encode key krsort ksort ltrim max method_exists min
    number_format ob_get_clean ob_start preg_match preg_match_all preg_quote preg_replace
    preg_replace_callback preg_split
    property_exists range rawurlencode reset round rsort rtrim serialize settype sort sprintf
    str_contains str_ends_with str_pad str_repeat str_replace str_split str_starts_with strcmp
    strip_tags stripslashes strlen strpos strrpos strtolower strtotime strtoupper strtr strval
    substr substr_count trim uasort uksort unserialize usort var_dump vsprintf wordwrap
    """.split()
)

BUILTIN_CALL_NAMES = frozenset(dir(builtins)) | _JS_GLOBALS | _PHP_BUILTINS


@dataclass(slots=True)
class _Node:
    id: int
    file_id: int
    name: str
    type: str
    parent_id: int | None


@dataclass(slots=True)
class _Import:
    module: str
    symbol: str | None
    alias: str | None
    level: int
    is_relative: bool
    is_reexport: bool

    @property
    def local_name(self) -> str | None:
        return self.alias or self.symbol


@dataclass(slots=True)
class _File:
    id: int
    path: str
    language: str
    imports: list[_Import] = field(default_factory=list)

    @property
    def dir(self) -> str:
        return self.path.rsplit("/", 1)[0] if "/" in self.path else ""


@dataclass(slots=True)
class ResolveStats:
    edges: int = 0
    resolved: int = 0
    heuristic: int = 0
    unresolved: int = 0
    external: int = 0
    dropped_builtins: int = 0


def resolve(db: Database, root: Path) -> ResolveStats:
    """Recompute every edge target.  Returns counts for the build summary."""
    return _Resolver(db, root).run()


class _Resolver:
    def __init__(self, db: Database, root: Path) -> None:
        self.db = db
        self.root = root
        self.files: dict[int, _File] = {}
        self.file_by_path: dict[str, _File] = {}
        self.file_by_lower: dict[str, _File] = {}
        self.nodes: dict[int, _Node] = {}
        self.nodes_by_file: dict[int, list[_Node]] = {}
        self.by_name: dict[str, list[_Node]] = {}
        self.module_node: dict[int, int] = {}
        self.python_modules: dict[str, list[_File]] = {}
        self.php_by_suffix: dict[str, list[_File]] = {}
        self.ts_aliases: list[tuple[str, str, list[str]]] = []

    # ------------------------------------------------------------------- run

    def run(self) -> ResolveStats:
        self._load()
        self._load_tsconfig()
        stats = ResolveStats()

        with self.db.transaction() as conn:
            conn.execute("UPDATE edges SET dst_id = NULL, resolved = 0, confidence = 'exact'")
            stats.dropped_builtins = self._drop_builtin_calls(conn)

            updates: list[tuple[int, str, int]] = []
            externals: list[tuple[int]] = []
            rows = conn.execute(
                "SELECT e.id, e.src_id, e.dst_name, e.dst_full, e.type, n.file_id "
                "FROM edges e JOIN nodes n ON n.id = e.src_id "
                "WHERE e.type IN ('calls','inherits','decorates','implements',"
                "'references','imports')"
            ).fetchall()

            for row in rows:
                stats.edges += 1
                source_file = self.files.get(row["file_id"])
                if source_file is None or not row["dst_name"]:
                    stats.unresolved += 1
                    continue
                if row["type"] == "imports":
                    target = self._resolve_import_edge(source_file, row["dst_name"])
                    # A non-relative module that does not resolve is a package
                    # outside the indexed tree, not a hole in the graph.
                    confidence = (
                        "external"
                        if target is None and not row["dst_name"].startswith(".")
                        else "exact"
                    )
                else:
                    target, confidence = self._resolve_symbol_edge(
                        source_file, row["src_id"], row["dst_name"], row["dst_full"]
                    )
                if target is None:
                    stats.unresolved += 1
                    if confidence == "external":
                        stats.external += 1
                        externals.append((row["id"],))
                    continue
                updates.append((target, confidence, row["id"]))
                stats.resolved += 1
                if confidence == "heuristic":
                    stats.heuristic += 1

            conn.executemany(
                "UPDATE edges SET dst_id = ?, resolved = 1, confidence = ? WHERE id = ?",
                updates,
            )
            conn.executemany("UPDATE edges SET confidence = 'external' WHERE id = ?", externals)
        return stats

    def _drop_builtin_calls(self, conn) -> int:
        """Delete ``calls`` edges to language builtins.

        ``len(x)`` and ``isinstance(y, Z)`` are a large fraction of all call
        edges and tell an agent nothing.  Only bare-name calls are dropped, and
        only for names the project does not declare itself, so a project with
        its own ``filter()`` keeps those edges.  The resolver is the first place
        that knows every project-wide name, which is why this lives here.
        """
        drop = sorted(BUILTIN_CALL_NAMES - set(self.by_name))
        if not drop:
            return 0
        placeholders = ",".join("?" * len(drop))
        cursor = conn.execute(
            f"DELETE FROM edges WHERE type = 'calls' AND dst_name IN ({placeholders}) "
            "AND (dst_full IS NULL OR dst_full = dst_name)",
            drop,
        )
        return cursor.rowcount or 0

    # ------------------------------------------------------------------ load

    def _load(self) -> None:
        conn = self.db.conn
        for row in conn.execute("SELECT id, path, language FROM files"):
            file = _File(id=row["id"], path=row["path"], language=row["language"])
            self.files[file.id] = file
            self.file_by_path[file.path] = file
            self.file_by_lower[file.path.lower()] = file

        for row in conn.execute(
            "SELECT file_id, module, symbol, alias, level, is_relative, is_reexport "
            "FROM imports ORDER BY id"
        ):
            file = self.files.get(row["file_id"])
            if file is not None:
                file.imports.append(
                    _Import(
                        module=row["module"],
                        symbol=row["symbol"],
                        alias=row["alias"],
                        level=row["level"],
                        is_relative=bool(row["is_relative"]),
                        is_reexport=bool(row["is_reexport"]),
                    )
                )

        for row in conn.execute("SELECT id, file_id, name, type, parent_id FROM nodes"):
            node = _Node(
                id=row["id"],
                file_id=row["file_id"],
                name=row["name"],
                type=row["type"],
                parent_id=row["parent_id"],
            )
            self.nodes[node.id] = node
            self.nodes_by_file.setdefault(node.file_id, []).append(node)
            if node.type == "module":
                self.module_node[node.file_id] = node.id
            else:
                self.by_name.setdefault(node.name, []).append(node)

        self._index_python_modules()
        self._index_php_paths()

    def _index_python_modules(self) -> None:
        """Map every dotted module name a Python file could be imported as."""
        for file in self.files.values():
            if file.language != "python":
                continue
            for key in python_module_keys(file.path):
                self.python_modules.setdefault(key, []).append(file)

    def _index_php_paths(self) -> None:
        """Index every trailing path fragment a PHP file could be required by.

        ``require_once Z52_CHILD_DIR . '/inc/x.php'`` keeps only the literal
        tail; the constant in front is unknowable from the source, so matching
        happens on the suffix.
        """
        for file in self.files.values():
            if file.language != "php":
                continue
            parts = file.path.split("/")
            for start in range(len(parts)):
                self.php_by_suffix.setdefault("/".join(parts[start:]), []).append(file)

    def _load_tsconfig(self) -> None:
        """``compilerOptions.paths`` aliases, if the project has a tsconfig."""
        for candidate in sorted(self.root.glob("**/tsconfig*.json")):
            if "node_modules" in candidate.as_posix():
                continue
            try:
                raw = candidate.read_text(encoding="utf-8", errors="replace")
                data = json.loads(_strip_json_comments(raw))
            except (OSError, ValueError):
                continue
            options = data.get("compilerOptions") or {}
            base_url = options.get("baseUrl") or "."
            paths = options.get("paths") or {}
            if not isinstance(paths, dict):
                continue
            base_dir = (candidate.parent / base_url).resolve()
            try:
                base_rel = base_dir.relative_to(self.root.resolve()).as_posix()
            except ValueError:
                base_rel = ""
            base_rel = "" if base_rel == "." else base_rel
            for pattern, targets in paths.items():
                if not isinstance(targets, list):
                    continue
                strings = [target for target in targets if isinstance(target, str)]
                self.ts_aliases.append((pattern, base_rel, strings))
        # Longest patterns win.
        self.ts_aliases.sort(key=lambda item: len(item[0]), reverse=True)

    # -------------------------------------------------------- edge resolution

    def _resolve_import_edge(self, source: _File, raw_module: str) -> int | None:
        target = self._resolve_module(source, raw_module)
        return self.module_node.get(target.id) if target else None

    def _resolve_symbol_edge(
        self, source: _File, src_id: int, dst_name: str, dst_full: str | None
    ) -> tuple[int | None, str]:
        # 0. `self.method()` inside a class: prefer a sibling member.
        if dst_full and dst_full.startswith(("self.", "this.")):
            sibling = self._sibling_member(src_id, dst_name)
            if sibling is not None:
                return sibling, "exact"

        # 1. Same file.
        local = self._in_file(source.id, dst_name)
        if local is not None:
            return local.id, "exact"

        # 2. Explicitly imported into this file.
        imported, external = self._via_import(source, dst_name, dst_full)
        if imported is not None:
            return imported, "exact"

        # 3. Globally unique name -- but only within the same language.  A
        # Python `db.add(...)` must never land on a TypeScript `add` just
        # because that name happens to be unique; a wrong edge is worse than a
        # missing one, because the agent cannot tell it is wrong.
        candidates = [
            node for node in self.by_name.get(dst_name, []) if self._same_language(source, node)
        ]
        if len(candidates) == 1:
            # PHP has a single global namespace for functions and classes, so a
            # project-wide unique name is not a guess -- it is how the language
            # resolves the call at runtime.
            certain = source.language == "php" and candidates[0].parent_id is None
            return candidates[0].id, "exact" if certain else "heuristic"

        # 4. Nothing -- but say so precisely.  A name imported from a module
        # outside the indexed tree (react, fastapi, sqlalchemy) is not a gap in
        # the graph, it is a third-party symbol that can never be resolved.
        #
        # PHP makes this exact rather than a guess: functions live in one global
        # namespace with no import mechanism, so a bare name the project does
        # not declare *must* come from outside it -- the language runtime,
        # WordPress core, a plugin.  Method calls still need a receiver type, so
        # they stay genuinely unknown.
        if not external and source.language == "php" and dst_full in (None, dst_name):
            external = True
        return None, "external" if external else "exact"

    def _same_language(self, source: _File, node: _Node) -> bool:
        target = self.files.get(node.file_id)
        if target is None:
            return False
        return _family(source.language) == _family(target.language)

    def _sibling_member(self, src_id: int, name: str) -> int | None:
        node = self.nodes.get(src_id)
        while node is not None and node.parent_id is not None:
            parent = self.nodes.get(node.parent_id)
            if parent is not None and parent.type == "class":
                for candidate in self.nodes_by_file.get(parent.file_id, []):
                    if candidate.parent_id == parent.id and candidate.name == name:
                        return candidate.id
                return None
            node = parent
        return None

    def _in_file(self, file_id: int, name: str) -> _Node | None:
        matches = [
            n for n in self.nodes_by_file.get(file_id, []) if n.name == name and n.type != "module"
        ]
        if not matches:
            return None
        top_level = [
            n
            for n in matches
            if n.parent_id is None or self.nodes.get(n.parent_id, _NULL).type == "module"
        ]
        return (top_level or matches)[0]

    def _via_import(
        self, source: _File, dst_name: str, dst_full: str | None
    ) -> tuple[int | None, bool]:
        """Resolve through this file's imports.

        Returns the target and whether the name was imported from a module that
        is not part of the indexed tree -- a third-party symbol, which is a
        different thing from a name we simply failed to find.
        """
        external = False
        for imp in source.imports:
            if imp.symbol in (None, "*"):
                continue
            if imp.local_name != dst_name:
                continue
            target = self._resolve_module(source, _raw_module(imp))
            if target is None:
                external = True
                continue
            found = self._find_symbol(target, imp.symbol, 0, set())
            if found is not None:
                return found, False

        # `api.fetchAll()` / `app.models.Task()` -- the chain's head is a module.
        if dst_full and "." in dst_full:
            head = dst_full.split(".", 1)[0]
            for imp in source.imports:
                if imp.symbol == "*" or (imp.symbol is None and imp.local_name in (None, head)):
                    module = _raw_module(imp)
                    if imp.local_name is None and not _module_matches_head(module, head, dst_full):
                        continue
                    target = self._resolve_module(source, module)
                    if target is None:
                        external = True
                        continue
                    found = self._find_symbol(target, dst_name, 0, set())
                    if found is not None:
                        return found, False
        return None, external

    # --------------------------------------------------------- symbol lookup

    def _find_symbol(
        self, file: _File, name: str, depth: int, seen: set[tuple[int, str]]
    ) -> int | None:
        """Find ``name`` in ``file``, following re-export chains."""
        if depth > MAX_REEXPORT_DEPTH or (file.id, name) in seen:
            return None
        seen.add((file.id, name))

        direct = self._in_file(file.id, name)
        if direct is not None:
            return direct.id

        for imp in file.imports:
            if imp.symbol in (None, "*") or imp.local_name != name:
                continue
            target = self._resolve_module(file, _raw_module(imp))
            if target is None or target.id == file.id:
                continue
            found = self._find_symbol(target, imp.symbol, depth + 1, seen)
            if found is not None:
                return found

        for imp in file.imports:
            if imp.symbol != "*":
                continue
            target = self._resolve_module(file, _raw_module(imp))
            if target is None or target.id == file.id:
                continue
            found = self._find_symbol(target, name, depth + 1, seen)
            if found is not None:
                return found

        return None

    # -------------------------------------------------------- module lookup

    def _resolve_module(self, source: _File, raw_module: str) -> _File | None:
        if source.language == "python":
            return self._resolve_python_module(source, raw_module)
        if source.language == "php":
            return self._resolve_php_module(source, raw_module)
        return self._resolve_ts_module(source, raw_module)

    def _resolve_php_module(self, source: _File, raw_module: str) -> _File | None:
        if "\\" in raw_module:
            return None  # a namespace, not a file: nothing on disk to point at
        cleaned = _normalize(raw_module)
        if not cleaned:
            return None

        direct = self._lookup_path(cleaned)
        if direct is not None:
            return direct
        if source.dir:
            near = self._lookup_path(_normalize(f"{source.dir}/{cleaned}"))
            if near is not None:
                return near
        candidates = self.php_by_suffix.get(cleaned)
        if candidates and len(candidates) == 1:
            return candidates[0]
        return None

    def _resolve_python_module(self, source: _File, raw_module: str) -> _File | None:
        level = len(raw_module) - len(raw_module.lstrip("."))
        module = raw_module[level:]

        if level:
            parts = source.dir.split("/") if source.dir else []
            if level > 1:
                parts = parts[: -(level - 1)] if level - 1 <= len(parts) else []
            base = "/".join(parts)
            tail = module.replace(".", "/")
            target = f"{base}/{tail}" if base and tail else (base or tail)
            return self._python_file_at(target)

        if not module:
            return None
        candidates = self.python_modules.get(module)
        if not candidates:
            # `app.models.Task` written as a module path when Task is a class.
            head = module.rsplit(".", 1)[0]
            candidates = self.python_modules.get(head) if "." in module else None
        if not candidates:
            return None
        return min(candidates, key=lambda f: (f.path.count("/"), f.path))

    def _python_file_at(self, target: str) -> _File | None:
        for extension in PY_EXTENSIONS:
            found = self._lookup_path(f"{target}{extension}")
            if found is not None:
                return found
        for extension in PY_EXTENSIONS:
            found = self._lookup_path(f"{target}/__init__{extension}")
            if found is not None:
                return found
        return None

    def _resolve_ts_module(self, source: _File, raw_module: str) -> _File | None:
        if raw_module.startswith("."):
            joined = _normalize(f"{source.dir}/{raw_module}" if source.dir else raw_module)
            return self._ts_file_at(joined)

        for pattern, base_rel, targets in self.ts_aliases:
            mapped = _apply_alias(pattern, targets, raw_module)
            for candidate in mapped:
                joined = _normalize(f"{base_rel}/{candidate}" if base_rel else candidate)
                found = self._ts_file_at(joined)
                if found is not None:
                    return found
        return None  # bare specifier -> node_modules, deliberately unresolved

    def _ts_file_at(self, target: str) -> _File | None:
        direct = self._lookup_path(target)
        if direct is not None:
            return direct
        stem = target
        for extension in (".js", ".jsx"):  # `import './x.js'` meaning ./x.ts
            if stem.endswith(extension):
                stem = stem[: -len(extension)]
                break
        for extension in TS_EXTENSIONS:
            found = self._lookup_path(f"{stem}{extension}")
            if found is not None:
                return found
        for extension in TS_EXTENSIONS:
            found = self._lookup_path(f"{stem}/index{extension}")
            if found is not None:
                return found
        return None

    def _lookup_path(self, path: str) -> _File | None:
        """Exact match first, then case-insensitive (Windows filesystems)."""
        found = self.file_by_path.get(path)
        if found is not None:
            return found
        return self.file_by_lower.get(path.lower())


_NULL = _Node(id=-1, file_id=-1, name="", type="", parent_id=None)


def python_module_keys(path: str) -> list[str]:
    """Every dotted name a Python file could be imported as.

    ``backend/app/models/task.py`` -> ``backend.app.models.task``,
    ``app.models.task``, ``models.task``, ``task``.  Indexing all suffixes means
    the tool does not need to be told where the package roots are.
    """
    for extension in PY_EXTENSIONS:
        if path.endswith(extension):
            path = path[: -len(extension)]
            break
    parts = path.split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return [".".join(parts[start:]) for start in range(len(parts)) if parts[start:]]


def _family(language: str) -> str:
    """Languages never resolve into one another; tsx counts as TypeScript."""
    if language in ("python", "php"):
        return language
    return "typescript"


def _raw_module(imp: _Import) -> str:
    return "." * imp.level + imp.module if imp.level else imp.module


def _module_matches_head(module: str, head: str, dst_full: str) -> bool:
    """``import a.b.c`` binds ``a``; also accept the full dotted prefix."""
    return module == head or module.split(".")[0] == head or dst_full.startswith(module + ".")


def _normalize(path: str) -> str:
    parts: list[str] = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def _apply_alias(pattern: str, targets: list[str], module: str) -> list[str]:
    if "*" in pattern:
        prefix, _, suffix = pattern.partition("*")
        if not module.startswith(prefix) or not module.endswith(suffix):
            return []
        middle = module[len(prefix) : len(module) - len(suffix) or None]
        return [target.replace("*", middle, 1) for target in targets]
    return list(targets) if module == pattern else []


_COMMENT_RE = re.compile(r"//[^\n\r]*|/\*.*?\*/", re.DOTALL)


def _strip_json_comments(text: str) -> str:
    """tsconfig.json is JSONC; drop comments and trailing commas."""
    without_comments = _COMMENT_RE.sub("", text)
    return re.sub(r",(\s*[}\]])", r"\1", without_comments)
