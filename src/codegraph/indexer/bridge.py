"""Linking frontend calls to backend handlers across the HTTP boundary.

Static analysis cannot cross an HTTP request, yet "which endpoint does this
page hit" and "who on the frontend uses this route" are the two questions asked
most often on a split frontend/backend codebase.  This pass reconstructs that
link by matching URL paths.

Isolated on purpose: everything framework-specific lives here, the pass is
skipped entirely when ``[bridge] enabled = false``, and supporting another
backend framework means adding one more ``_scan_*`` function.

Two halves:

* backend -- FastAPI router decorators become ``endpoint`` nodes, with
  ``APIRouter(prefix=...)`` and ``include_router(..., prefix=...)`` folded into
  the final path, plus a ``handles`` edge endpoint -> handler function;
* frontend -- string/template literals that look like API paths inside
  ``frontend_api_dir`` become ``calls_api`` edges from the enclosing function.

Both sides are normalised to ``/tasks/:param`` before matching, so
``/tasks/{task_id}`` and ``/tasks/${id}`` line up.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from ..config import Config
from ..db import Database
from .resolver import python_module_keys
from .walker import read_source

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")

# A literal that plausibly names an API path.
_PATH_LIKE = re.compile(r"^/[^\s?#]*$")
_TEMPLATE_HOLE = re.compile(r"\$\{[^}]*\}")
_PATH_PARAM = re.compile(r"\{[^}]*\}")


@dataclass(slots=True)
class BridgeStats:
    endpoints: int = 0
    handled: int = 0
    api_calls: int = 0
    api_calls_matched: int = 0


@dataclass(slots=True)
class _Route:
    method: str
    path: str  # as written on the decorator
    handler: str  # handler function name
    line: int
    file_path: str
    router_var: str


@dataclass(slots=True)
class _Router:
    file_path: str
    var: str
    prefix: str
    is_app: bool = False


@dataclass(slots=True)
class _Inclusion:
    parent: tuple[str, str]  # (file_path, var) of the including router/app
    child_hint: str  # module hint from `pkg.router`, '' when a bare name
    child_var: str
    prefix: str


@dataclass(slots=True)
class _Endpoint:
    node_id: int
    method: str
    normalized: str


def run_bridge(db: Database, config: Config) -> BridgeStats:
    """Build endpoint nodes and the edges around them.  Idempotent."""
    stats = BridgeStats()
    if not config.bridge.enabled:
        return stats

    with db.transaction() as conn:
        conn.execute(
            "DELETE FROM edges WHERE type IN ('handles', 'calls_api') "
            "OR dst_id IN (SELECT id FROM nodes WHERE type = 'endpoint')"
        )
        conn.execute("DELETE FROM nodes WHERE type = 'endpoint'")

        endpoints = _index_backend(db, config, stats)
        if config.bridge.frontend_api_dir:
            _index_frontend(db, config, endpoints, stats)

    return stats


# --------------------------------------------------------------------- backend


def _index_backend(db: Database, config: Config, stats: BridgeStats) -> list[_Endpoint]:
    python_files = {
        row["path"]: row["id"]
        for row in db.conn.execute("SELECT id, path FROM files WHERE language = 'python'")
    }

    routers: dict[tuple[str, str], _Router] = {}
    inclusions: list[_Inclusion] = []
    routes: list[_Route] = []

    for rel_path in python_files:
        abs_path = config.root / rel_path
        try:
            tree = ast.parse(read_source(abs_path), filename=rel_path)
        except (OSError, SyntaxError, ValueError):
            continue
        _scan_fastapi_module(tree, rel_path, routers, inclusions, routes)

    prefixes = _resolve_prefixes(routers, inclusions, _import_aliases(db))

    endpoints: list[_Endpoint] = []
    for route in routes:
        prefix = prefixes.get((route.file_path, route.router_var), "")
        full_path = _join_path(prefix, route.path)
        file_id = python_files[route.file_path]
        node_id = _insert_endpoint(db, file_id, route, full_path)
        stats.endpoints += 1
        endpoints.append(
            _Endpoint(node_id=node_id, method=route.method, normalized=_normalize_path(full_path))
        )
        handler_id = _handler_node_id(db, file_id, route.handler, route.line)
        db.conn.execute(
            "INSERT INTO edges(src_id, dst_id, dst_name, dst_full, type, line, resolved, "
            "confidence) VALUES (?, ?, ?, ?, 'handles', ?, ?, 'exact')",
            (
                node_id,
                handler_id,
                route.handler,
                f"{route.file_path}::{route.handler}",
                route.line,
                1 if handler_id else 0,
            ),
        )
        if handler_id:
            stats.handled += 1
    return endpoints


def _scan_fastapi_module(
    tree: ast.Module,
    rel_path: str,
    routers: dict[tuple[str, str], _Router],
    inclusions: list[_Inclusion],
    routes: list[_Route],
) -> None:
    for stmt in ast.walk(tree):
        if isinstance(stmt, ast.Assign | ast.AnnAssign):
            _scan_router_assignment(stmt, rel_path, routers)
        elif isinstance(stmt, ast.Call):
            _scan_include_router(stmt, rel_path, inclusions)
        elif isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
            _scan_route_decorators(stmt, rel_path, routes)


def _scan_router_assignment(
    stmt: ast.Assign | ast.AnnAssign,
    rel_path: str,
    routers: dict[tuple[str, str], _Router],
) -> None:
    targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
    value = stmt.value
    if not isinstance(value, ast.Call):
        return
    factory = _short_name(value.func)
    if factory not in ("APIRouter", "FastAPI"):
        return
    prefix = _string_keyword(value, "prefix") or ""
    for target in targets:
        if isinstance(target, ast.Name):
            routers[(rel_path, target.id)] = _Router(
                file_path=rel_path,
                var=target.id,
                prefix=prefix,
                is_app=factory == "FastAPI",
            )


def _scan_include_router(call: ast.Call, rel_path: str, inclusions: list[_Inclusion]) -> None:
    if _short_name(call.func) != "include_router":
        return
    parent_var = _receiver_name(call.func)
    if parent_var is None or not call.args:
        return
    hint, child_var = _split_router_arg(call.args[0])
    if child_var is None:
        return
    prefix = _string_keyword(call, "prefix") or ""
    inclusions.append(
        _Inclusion(
            parent=(rel_path, parent_var),
            child_hint=hint,
            child_var=child_var,
            prefix=prefix,
        )
    )


def _scan_route_decorators(
    stmt: ast.FunctionDef | ast.AsyncFunctionDef, rel_path: str, routes: list[_Route]
) -> None:
    for decorator in stmt.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        func = decorator.func
        if not isinstance(func, ast.Attribute) or func.attr not in HTTP_METHODS:
            continue
        router_var = _receiver_name(func)
        if router_var is None:
            continue
        path = _string_keyword(decorator, "path") or _first_string_argument(decorator)
        if path is None:
            continue
        routes.append(
            _Route(
                method=func.attr.upper(),
                path=path,
                handler=stmt.name,
                line=getattr(decorator, "lineno", stmt.lineno),
                file_path=rel_path,
                router_var=router_var,
            )
        )


def _resolve_prefixes(
    routers: dict[tuple[str, str], _Router],
    inclusions: list[_Inclusion],
    aliases: dict[str, dict[str, str]],
) -> dict[tuple[str, str], str]:
    """Combine a router's own prefix with every prefix it is included under."""
    module_index: dict[str, list[str]] = {}
    for file_path, _ in routers:
        for key in python_module_keys(file_path):
            module_index.setdefault(key, []).append(file_path)

    # child key -> (parent key, prefix contributed by the include_router call)
    parents: dict[tuple[str, str], tuple[tuple[str, str], str]] = {}
    for inclusion in inclusions:
        child = _match_child_router(inclusion, routers, module_index, aliases)
        if child is None or child in parents:
            continue
        parents[child] = (inclusion.parent, inclusion.prefix)

    resolved: dict[tuple[str, str], str] = {}
    for key, router in routers.items():
        chain: list[str] = [router.prefix]
        current = key
        for _ in range(8):  # depth guard, also breaks include cycles
            entry = parents.get(current)
            if entry is None:
                break
            parent_key, prefix = entry
            parent = routers.get(parent_key)
            chain.append(prefix)
            if parent is not None:
                chain.append(parent.prefix)
            if parent_key == current:
                break
            current = parent_key
        resolved[key] = "".join(part.rstrip("/") for part in reversed(chain) if part)
    return resolved


def _match_child_router(
    inclusion: _Inclusion,
    routers: dict[tuple[str, str], _Router],
    module_index: dict[str, list[str]],
    aliases: dict[str, dict[str, str]],
) -> tuple[str, str] | None:
    """Find the router that ``include_router(<hint>.<var>, ...)`` refers to."""
    if not inclusion.child_hint:
        same_file = (inclusion.parent[0], inclusion.child_var)
        if same_file in routers:
            return same_file
        candidates = [key for key in routers if key[1] == inclusion.child_var]
        return candidates[0] if len(candidates) == 1 else None

    # `from app.api import settings as settings_router` -- the hint is a local
    # alias, so consult the importing file's bindings before guessing by name.
    hints = [inclusion.child_hint]
    aliased = aliases.get(inclusion.parent[0], {}).get(inclusion.child_hint)
    if aliased:
        hints.insert(0, aliased)
    hints.append(inclusion.child_hint.rsplit(".", 1)[-1])

    for hint in hints:
        for file_path in module_index.get(hint, []):
            key = (file_path, inclusion.child_var)
            if key in routers:
                return key
    return None


def _import_aliases(db: Database) -> dict[str, dict[str, str]]:
    """Per file, the local name each imported module is bound to.

    ``from app.api import settings as settings_router`` yields
    ``{'settings_router': 'app.api.settings'}``, which is what turns an aliased
    ``include_router(settings_router.router, ...)`` into a real match.
    """
    aliases: dict[str, dict[str, str]] = {}
    rows = db.conn.execute(
        "SELECT f.path, i.module, i.symbol, i.alias FROM imports i "
        "JOIN files f ON f.id = i.file_id WHERE f.language = 'python'"
    ).fetchall()
    for row in rows:
        local = row["alias"] or row["symbol"]
        if not local:
            continue
        module = f"{row['module']}.{row['symbol']}" if row["symbol"] else row["module"]
        aliases.setdefault(row["path"], {})[local] = module.lstrip(".")
    return aliases


def _insert_endpoint(db: Database, file_id: int, route: _Route, full_path: str) -> int:
    cursor = db.conn.execute(
        "INSERT INTO nodes(file_id, type, name, qualified_name, parent_id, line_start, "
        "line_end, signature, docstring, is_exported, is_async) "
        "VALUES (?, 'endpoint', ?, ?, NULL, ?, ?, ?, NULL, 1, 0)",
        (
            file_id,
            full_path or "/",
            f"{route.method} {full_path or '/'}",
            route.line,
            route.line,
            f"{route.method} {full_path or '/'} -> {route.handler}()",
        ),
    )
    return int(cursor.lastrowid)


def _handler_node_id(db: Database, file_id: int, name: str, line: int) -> int | None:
    row = db.conn.execute(
        "SELECT id FROM nodes WHERE file_id = ? AND name = ? AND type IN ('function','method') "
        "ORDER BY abs(line_start - ?) LIMIT 1",
        (file_id, name, line),
    ).fetchone()
    return int(row["id"]) if row else None


# -------------------------------------------------------------------- frontend


def _index_frontend(
    db: Database, config: Config, endpoints: list[_Endpoint], stats: BridgeStats
) -> None:
    api_dir = (config.bridge.frontend_api_dir or "").strip("/")
    rows = db.conn.execute(
        "SELECT id, path, language FROM files WHERE language IN ('typescript', 'tsx')"
    ).fetchall()

    for row in rows:
        rel_path: str = row["path"]
        if api_dir and not rel_path.startswith(api_dir + "/") and rel_path != api_dir:
            continue
        abs_path = config.root / rel_path
        try:
            source = read_source(abs_path)
        except OSError:
            continue
        for method, raw_path, line in _find_api_calls(source, row["language"]):
            stats.api_calls += 1
            src_id = _enclosing_node_id(db, row["id"], line)
            if src_id is None:
                continue
            match, confidence = _match_endpoint(endpoints, method, raw_path)
            db.conn.execute(
                "INSERT INTO edges(src_id, dst_id, dst_name, dst_full, type, line, resolved, "
                "confidence) VALUES (?, ?, ?, ?, 'calls_api', ?, ?, ?)",
                (
                    src_id,
                    match,
                    f"{method} {raw_path}" if method else raw_path,
                    raw_path,
                    line,
                    1 if match else 0,
                    confidence,
                ),
            )
            if match:
                stats.api_calls_matched += 1


def _find_api_calls(source: str, language: str) -> list[tuple[str | None, str, int]]:
    """``client.get('/tasks')`` / ``fetch(`/tasks/${id}`)`` -> (METHOD, path, line)."""
    from .ts_parser import parse_tree  # local import: keeps grammar loading lazy

    kind = "tsx" if language == "tsx" else "typescript"
    tree, data = parse_tree(kind, source.encode("utf-8"))

    found: list[tuple[str | None, str, int]] = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        for child in node.named_children:
            stack.append(child)
        if node.type != "call_expression":
            continue
        callee = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if callee is None or arguments is None:
            continue
        method = _http_method_of(callee, data)
        if method is None:
            continue
        for argument in arguments.named_children:
            literal = _string_literal(argument, data)
            if literal and _PATH_LIKE.match(literal):
                found.append((method or None, literal, node.start_point[0] + 1))
                break
    return found


def _http_method_of(callee, data: bytes) -> str | None:
    """Returns the HTTP verb, ``''`` for a generic fetch, ``None`` if not a request."""
    text = data[callee.start_byte : callee.end_byte].decode("utf-8", "replace")
    tail = text.rsplit(".", 1)[-1]
    if tail.lower() in HTTP_METHODS:
        return tail.upper()
    if tail in ("fetch", "request"):
        return ""
    return None


def _string_literal(node, data: bytes) -> str | None:
    if node.type == "string":
        fragment = next((c for c in node.named_children if c.type == "string_fragment"), None)
        if fragment is None:
            return ""
        return data[fragment.start_byte : fragment.end_byte].decode("utf-8", "replace")
    if node.type == "template_string":
        text = data[node.start_byte : node.end_byte].decode("utf-8", "replace")
        return text.strip("`")
    return None


def _enclosing_node_id(db: Database, file_id: int, line: int) -> int | None:
    """The innermost function/method/component whose line range covers ``line``."""
    row = db.conn.execute(
        "SELECT id FROM nodes WHERE file_id = ? AND line_start <= ? AND line_end >= ? "
        "AND type IN ('function','method','component','class') "
        "ORDER BY (line_end - line_start) ASC LIMIT 1",
        (file_id, line, line),
    ).fetchone()
    if row:
        return int(row["id"])
    module = db.conn.execute(
        "SELECT id FROM nodes WHERE file_id = ? AND type = 'module' LIMIT 1", (file_id,)
    ).fetchone()
    return int(module["id"]) if module else None


def _match_endpoint(
    endpoints: list[_Endpoint], method: str | None, raw_path: str
) -> tuple[int | None, str]:
    normalized = _normalize_path(raw_path)
    exact = [
        endpoint
        for endpoint in endpoints
        if endpoint.normalized == normalized and (not method or endpoint.method == method)
    ]
    if len(exact) == 1:
        return exact[0].node_id, "exact"
    if len(exact) > 1:
        return exact[0].node_id, "heuristic"

    # The frontend client usually carries a baseURL the backend path does not
    # know about (`/api`), so fall back to matching on whole trailing segments.
    # `/users` has to reach `/api/users` without being confused by
    # `/api/design-requests/users`, so candidates are ranked by how many leading
    # segments had to be skipped -- fewest wins, ties stay unresolved.
    scored: list[tuple[int, _Endpoint]] = []
    for endpoint in endpoints:
        if method and endpoint.method != method:
            continue
        skipped = _segments_skipped(endpoint.normalized, normalized)
        if skipped is not None:
            scored.append((skipped, endpoint))
    if not scored:
        return None, "heuristic"
    best = min(score for score, _ in scored)
    winners = [endpoint for score, endpoint in scored if score == best]
    if len(winners) == 1:
        return winners[0].node_id, "heuristic"
    return None, "heuristic"


def _segments_skipped(endpoint_path: str, called_path: str) -> int | None:
    """How many leading segments separate the two paths, or ``None`` if unrelated.

    Only whole-segment suffixes count, so ``/users`` matches ``/api/users`` but
    not ``/api/superusers``.
    """
    long, short = endpoint_path, called_path
    if len(short) > len(long):
        long, short = short, long
    if long == short:
        return 0
    if not long.endswith(short):
        return None
    head = long[: -len(short)]
    if short.startswith("/") and not head.endswith("/"):
        return head.count("/")
    return None


# --------------------------------------------------------------------- shared


def _normalize_path(path: str) -> str:
    """``/tasks/{task_id}`` and ``/tasks/${id}`` both become ``/tasks/:param``."""
    path = _TEMPLATE_HOLE.sub(":param", path)
    path = _PATH_PARAM.sub(":param", path)
    path = re.sub(r"/:[^/]+", "/:param", path)
    path = re.sub(r"/+", "/", path)
    if len(path) > 1:
        path = path.rstrip("/")
    return path or "/"


def _join_path(prefix: str, path: str) -> str:
    prefix = prefix.rstrip("/")
    if not path or path == "/":
        return prefix or "/"
    if not path.startswith("/"):
        path = "/" + path
    return f"{prefix}{path}"


def _string_keyword(call: ast.Call, name: str) -> str | None:
    for keyword in call.keywords:
        value = keyword.value
        if keyword.arg == name and isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
    return None


def _first_string_argument(call: ast.Call) -> str | None:
    if call.args and isinstance(call.args[0], ast.Constant):
        value = call.args[0].value
        if isinstance(value, str):
            return value
    return None


def _short_name(expr: ast.expr) -> str | None:
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return expr.attr
    return None


def _receiver_name(expr: ast.expr) -> str | None:
    """``router`` from ``router.get`` / ``app.include_router``."""
    if isinstance(expr, ast.Attribute):
        value = expr.value
        if isinstance(value, ast.Name):
            return value.id
        if isinstance(value, ast.Attribute):
            return value.attr
    return None


def _split_router_arg(expr: ast.expr) -> tuple[str, str | None]:
    """``tasks.router`` -> ('tasks', 'router'); ``router`` -> ('', 'router')."""
    if isinstance(expr, ast.Name):
        return "", expr.id
    if isinstance(expr, ast.Attribute):
        parts: list[str] = []
        current: ast.expr = expr
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
        parts.reverse()
        return ".".join(parts[:-1]), parts[-1]
    return "", None
