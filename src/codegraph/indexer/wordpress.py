"""Linking a WordPress theme together through its string-based indirection.

WordPress wires itself with strings, and a call graph cannot see strings:

* ``add_action('wp_enqueue_scripts', $callback)`` -- nothing calls the callback;
  the core dispatcher does, at a moment named by a string;
* ``get_template_part('template-parts/hero')`` -- a file inclusion written as a
  slug, so template composition is invisible;
* ``add_action('wp_ajax_thing', 'handler')`` -- an HTTP endpoint hiding inside a
  hook name.

Exactly the same class of problem as the HTTP boundary between a frontend and
its backend, and solved the same way: a separate pass that reads the wiring and
writes the edges the language cannot express.  Enabled with
``[bridge] backend_framework = "wordpress"``.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Config
from ..db import Database
from .walker import read_source

HOOK_REGISTRARS = {"add_action": "action", "add_filter": "filter"}
AJAX_PREFIXES = ("wp_ajax_nopriv_", "wp_ajax_")
TEMPLATE_CALLS = ("get_template_part", "get_header", "get_footer", "get_sidebar")

_CLOSURE_TYPES = frozenset({"anonymous_function", "anonymous_function_creation_expression"})


@dataclass(slots=True)
class WordPressStats:
    hooks: int = 0
    hooks_handled: int = 0
    ajax_endpoints: int = 0
    rest_endpoints: int = 0
    templates: int = 0
    templates_matched: int = 0


@dataclass(slots=True)
class _Registration:
    kind: str  # 'action' | 'filter'
    hook: str
    line: int
    callback_name: str | None  # a named function, when the callback is a string
    callback_line: int | None  # a closure's own first line, when it is inline


def run_wordpress(db: Database, config: Config) -> WordPressStats:
    """Build hook, AJAX and template edges.  Idempotent."""
    from .php_parser import _parser  # local import keeps the grammar lazy

    stats = WordPressStats()
    php_files = {
        row["path"]: row["id"]
        for row in db.conn.execute("SELECT id, path FROM files WHERE language = 'php'")
    }
    if not php_files:
        return stats

    parser = _parser()
    for rel_path, file_id in php_files.items():
        try:
            data = read_source(config.root / rel_path).encode("utf-8")
        except OSError:
            continue
        tree = parser.parse(data)
        scanner = _Scanner(data)
        scanner.walk(tree.root_node)

        for registration in scanner.registrations:
            _write_hook(db, file_id, rel_path, registration, stats)
        for route in scanner.rest_routes:
            _write_rest_route(db, file_id, route, stats)
        for slug, line in scanner.template_parts:
            _write_template_edge(db, file_id, slug, line, php_files, stats)

    return stats


class _Scanner:
    """Collects the WordPress wiring out of one parsed file."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.registrations: list[_Registration] = []
        self.rest_routes: list[tuple[str, int]] = []
        self.template_parts: list[tuple[str, int]] = []

    def walk(self, root) -> None:
        stack = [root]
        while stack:
            node = stack.pop()
            for child in node.named_children:
                stack.append(child)
            if node.type != "function_call_expression":
                continue
            callee = self._text(node.child_by_field_name("function"))
            arguments = node.child_by_field_name("arguments")
            if arguments is None:
                continue
            if callee in HOOK_REGISTRARS:
                self._hook(node, callee, arguments)
            elif callee == "register_rest_route":
                self._rest_route(node, arguments)
            elif callee in TEMPLATE_CALLS:
                self._template_part(node, callee, arguments)

    def _hook(self, call, callee: str, arguments) -> None:
        parts = list(arguments.named_children)
        if not parts:
            return
        hook = self._string_of(parts[0])
        if not hook:
            return  # a computed hook name; nothing stable to key on

        callback_name: str | None = None
        callback_line: int | None = None
        if len(parts) > 1:
            second = parts[1]
            closure = _find_closure(second)
            if closure is not None:
                callback_line = closure.start_point[0] + 1
            else:
                callback_name = self._string_of(second)
                if callback_name and "::" in callback_name:
                    callback_name = callback_name.rsplit("::", 1)[-1]

        self.registrations.append(
            _Registration(
                kind=HOOK_REGISTRARS[callee],
                hook=hook,
                line=call.start_point[0] + 1,
                callback_name=callback_name,
                callback_line=callback_line,
            )
        )

    def _rest_route(self, call, arguments) -> None:
        parts = list(arguments.named_children)
        if len(parts) < 2:
            return
        namespace = self._string_of(parts[0]) or ""
        route = self._string_of(parts[1]) or ""
        if namespace or route:
            joined = f"/{namespace.strip('/')}/{route.strip('/')}".replace("//", "/")
            self.rest_routes.append((joined, call.start_point[0] + 1))

    def _template_part(self, call, callee: str, arguments) -> None:
        parts = list(arguments.named_children)
        if callee != "get_template_part":
            # get_header('shop') -> header-shop.php; bare -> header.php
            base = callee.removeprefix("get_")
            suffix = self._string_of(parts[0]) if parts else None
            slug = f"{base}-{suffix}" if suffix else base
            self.template_parts.append((slug, call.start_point[0] + 1))
            return
        if not parts:
            return
        slug = self._string_of(parts[0])
        if not slug:
            return
        name = self._string_of(parts[1]) if len(parts) > 1 else None
        self.template_parts.append((f"{slug}-{name}" if name else slug, call.start_point[0] + 1))

    def _string_of(self, node) -> str | None:
        """The literal text of an argument, if it is a plain string."""
        stack = [node]
        while stack:
            current = stack.pop(0)
            if current.type in _CLOSURE_TYPES:
                return None
            if current.type in ("string", "encapsed_string"):
                fragment = next(
                    (c for c in current.named_children if c.type == "string_content"), None
                )
                return self._text(fragment) if fragment is not None else ""
            stack.extend(current.named_children)
        return None

    def _text(self, node) -> str:
        if node is None:
            return ""
        return self.data[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _find_closure(node):
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in _CLOSURE_TYPES:
            return current
        stack.extend(current.named_children)
    return None


# ----------------------------------------------------------------- persistence


def _write_hook(
    db: Database,
    file_id: int,
    rel_path: str,
    registration: _Registration,
    stats: WordPressStats,
) -> None:
    qualified = f"{registration.kind} {registration.hook}"
    node_type = "endpoint" if _ajax_action(registration.hook) else "hook"
    if node_type == "endpoint":
        qualified = f"AJAX {_ajax_action(registration.hook)}"
        stats.ajax_endpoints += 1
    else:
        stats.hooks += 1

    cursor = db.conn.execute(
        "INSERT INTO nodes(file_id, type, name, qualified_name, parent_id, line_start, "
        "line_end, signature, docstring, is_exported, is_async) "
        "VALUES (?, ?, ?, ?, NULL, ?, ?, ?, NULL, 1, 0)",
        (
            file_id,
            node_type,
            registration.hook,
            qualified,
            registration.line,
            registration.line,
            f"{qualified} -> {registration.callback_name or 'closure'}",
        ),
    )
    hook_id = int(cursor.lastrowid)

    target = _callback_node_id(db, file_id, registration)
    db.conn.execute(
        "INSERT INTO edges(src_id, dst_id, dst_name, dst_full, type, line, resolved, "
        "confidence) VALUES (?, ?, ?, ?, 'handles', ?, ?, 'exact')",
        (
            hook_id,
            target,
            registration.callback_name or "closure",
            f"{rel_path}:{registration.callback_line or registration.line}",
            registration.line,
            1 if target else 0,
        ),
    )
    if target:
        stats.hooks_handled += 1


def _callback_node_id(db: Database, file_id: int, registration: _Registration) -> int | None:
    """The declaration a hook actually runs.

    An inline closure is matched on the line it opens, which is the same line
    the parser recorded for it -- the two passes read the same tree, so the join
    is exact rather than a guess.
    """
    if registration.callback_line is not None:
        row = db.conn.execute(
            "SELECT id FROM nodes WHERE file_id = ? AND line_start = ? "
            "AND type IN ('function', 'method') ORDER BY id LIMIT 1",
            (file_id, registration.callback_line),
        ).fetchone()
        return int(row["id"]) if row else None

    if not registration.callback_name:
        return None
    row = db.conn.execute(
        "SELECT n.id FROM nodes n JOIN files f ON f.id = n.file_id "
        "WHERE n.name = ? AND n.type IN ('function', 'method') AND f.language = 'php' "
        "ORDER BY (n.file_id != ?) LIMIT 1",
        (registration.callback_name, file_id),
    ).fetchone()
    return int(row["id"]) if row else None


def _write_rest_route(
    db: Database, file_id: int, route: tuple[str, int], stats: WordPressStats
) -> None:
    path, line = route
    db.conn.execute(
        "INSERT INTO nodes(file_id, type, name, qualified_name, parent_id, line_start, "
        "line_end, signature, docstring, is_exported, is_async) "
        "VALUES (?, 'endpoint', ?, ?, NULL, ?, ?, ?, NULL, 1, 0)",
        (file_id, path, f"REST {path}", line, line, f"REST {path}"),
    )
    stats.rest_endpoints += 1


def _write_template_edge(
    db: Database,
    file_id: int,
    slug: str,
    line: int,
    php_files: dict[str, int],
    stats: WordPressStats,
) -> None:
    """``get_template_part('template-parts/hero')`` -> the file it pulls in."""
    stats.templates += 1
    target_path = _match_template(slug, php_files)
    target_id = None
    if target_path is not None:
        row = db.conn.execute(
            "SELECT id FROM nodes WHERE file_id = ? AND type = 'module' LIMIT 1",
            (php_files[target_path],),
        ).fetchone()
        target_id = int(row["id"]) if row else None

    source_id = _enclosing_declaration(db, file_id, line)
    if source_id is None:
        return
    db.conn.execute(
        "INSERT INTO edges(src_id, dst_id, dst_name, dst_full, type, line, resolved, "
        "confidence) VALUES (?, ?, ?, ?, 'renders', ?, ?, 'exact')",
        (source_id, target_id, slug, target_path or slug, line, 1 if target_id else 0),
    )
    if target_id:
        stats.templates_matched += 1


def _match_template(slug: str, php_files: dict[str, int]) -> str | None:
    """A slug names a file relative to the theme root; fall back to a suffix."""
    wanted = f"{slug.strip('/')}.php"
    if wanted in php_files:
        return wanted
    matches = [path for path in php_files if path.endswith(f"/{wanted}")]
    return matches[0] if len(matches) == 1 else None


def _enclosing_declaration(db: Database, file_id: int, line: int) -> int | None:
    row = db.conn.execute(
        "SELECT id FROM nodes WHERE file_id = ? AND line_start <= ? AND line_end >= ? "
        "AND type != 'module' ORDER BY (line_end - line_start) ASC LIMIT 1",
        (file_id, line, line),
    ).fetchone()
    if row:
        return int(row["id"])
    module = db.conn.execute(
        "SELECT id FROM nodes WHERE file_id = ? AND type = 'module' LIMIT 1", (file_id,)
    ).fetchone()
    return int(module["id"]) if module else None


def _ajax_action(hook: str) -> str | None:
    """``wp_ajax_thing`` is an HTTP endpoint wearing a hook's clothes."""
    for prefix in AJAX_PREFIXES:
        if hook.startswith(prefix) and len(hook) > len(prefix):
            return hook[len(prefix) :]
    return None
