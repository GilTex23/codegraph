"""Python parser built on the stdlib ``ast`` module.

Extracts modules, classes, functions/methods, module-level constants, and the
``calls`` / ``imports`` / ``inherits`` / ``decorates`` edges between them.  A
file that does not parse yields just its module node plus an error entry --
never an exception.
"""

from __future__ import annotations

import ast

from ..models import NodeBuilder, ParseResult, clip_docstring

MAX_VALUE_PREVIEW = 80


def parse_python(rel_path: str, source: str) -> ParseResult:
    """Parse one Python file into nodes, edges and import records."""
    builder = NodeBuilder()
    builder.result.language = "python"

    module_name = rel_path.rsplit("/", 1)[-1].removesuffix(".py").removesuffix(".pyi")
    line_count = source.count("\n") + 1

    try:
        tree = ast.parse(source, filename=rel_path)
    except (SyntaxError, ValueError) as exc:
        lineno = getattr(exc, "lineno", None) or 1
        builder.result.errors.append(f"{rel_path}:{lineno}: {exc}")
        builder.add_node(
            type="module",
            name=module_name,
            qualified_name=rel_path,
            line_start=1,
            line_end=line_count,
        )
        return builder.result

    module = builder.add_node(
        type="module",
        name=module_name,
        qualified_name=rel_path,
        line_start=1,
        line_end=line_count,
        docstring=clip_docstring(ast.get_docstring(tree)),
    )

    _Extractor(
        builder,
        rel_path,
        module.local_id,
        is_package_init=rel_path.endswith("__init__.py"),
    ).run(tree)
    return builder.result


class _Extractor:
    """Recursive descent over the AST, tracking the enclosing declaration."""

    def __init__(
        self,
        builder: NodeBuilder,
        rel_path: str,
        module_local: int,
        *,
        is_package_init: bool,
    ) -> None:
        self.b = builder
        self.rel_path = rel_path
        self.module_local = module_local
        self.is_package_init = is_package_init
        self.exported: set[str] | None = None

    def run(self, tree: ast.Module) -> None:
        self.exported = _read_dunder_all(tree)
        self._body(tree.body, parent=None, owner=self.module_local, scope="", top_level=True)

    # ------------------------------------------------------------------ body

    def _body(
        self,
        body: list[ast.stmt],
        *,
        parent: int | None,
        owner: int,
        scope: str,
        top_level: bool = False,
        in_class: bool = False,
        collect_calls: bool = True,
    ) -> None:
        for stmt in body:
            if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
                self._function(stmt, parent=parent, scope=scope, in_class=in_class)
            elif isinstance(stmt, ast.ClassDef):
                self._class(stmt, parent=parent, scope=scope)
            elif isinstance(stmt, ast.Import | ast.ImportFrom):
                self._import(stmt)
            else:
                if top_level:
                    self._module_constant(stmt)
                # A whole statement is scanned once, at the outermost level that
                # owns it; the nested pass below only looks for declarations, so
                # calls inside if/try/with bodies are not counted twice.
                if collect_calls:
                    self._calls_in(stmt, owner)
                self._recurse_nested(
                    stmt,
                    parent=parent,
                    owner=owner,
                    scope=scope,
                    top_level=top_level,
                    in_class=in_class,
                )

    def _recurse_nested(
        self,
        stmt: ast.stmt,
        *,
        parent: int | None,
        owner: int,
        scope: str,
        top_level: bool,
        in_class: bool,
    ) -> None:
        """Descend into compound statements (if/try/with/for) that hold declarations."""
        for field in ("body", "orelse", "finalbody"):
            inner = getattr(stmt, field, None)
            if isinstance(inner, list) and inner and isinstance(inner[0], ast.stmt):
                self._body(
                    inner,
                    parent=parent,
                    owner=owner,
                    scope=scope,
                    top_level=top_level,
                    in_class=in_class,
                    collect_calls=False,
                )
        for handler in getattr(stmt, "handlers", []) or []:
            self._body(
                handler.body,
                parent=parent,
                owner=owner,
                scope=scope,
                top_level=top_level,
                in_class=in_class,
                collect_calls=False,
            )
        for case in getattr(stmt, "cases", []) or []:
            self._body(
                case.body,
                parent=parent,
                owner=owner,
                scope=scope,
                top_level=top_level,
                in_class=in_class,
                collect_calls=False,
            )

    # ------------------------------------------------------------ declarations

    def _function(
        self,
        stmt: ast.FunctionDef | ast.AsyncFunctionDef,
        *,
        parent: int | None,
        scope: str,
        in_class: bool,
    ) -> None:
        qualified = f"{self.rel_path}::{scope}{stmt.name}"
        node = self.b.add_node(
            type="method" if in_class else "function",
            name=stmt.name,
            qualified_name=qualified,
            parent_local=parent,
            line_start=_start_line(stmt),
            line_end=stmt.end_lineno or stmt.lineno,
            signature=_function_signature(stmt),
            docstring=clip_docstring(ast.get_docstring(stmt)),
            is_exported=self._is_exported(stmt.name, top_level=parent is None),
            is_async=isinstance(stmt, ast.AsyncFunctionDef),
        )
        self._decorators(stmt, node.local_id)
        for default in [*stmt.args.defaults, *[d for d in stmt.args.kw_defaults if d is not None]]:
            self._calls_in(default, node.local_id)
        self._body(
            stmt.body,
            parent=node.local_id,
            owner=node.local_id,
            scope=f"{scope}{stmt.name}.",
        )

    def _class(self, stmt: ast.ClassDef, *, parent: int | None, scope: str) -> None:
        qualified = f"{self.rel_path}::{scope}{stmt.name}"
        node = self.b.add_node(
            type="class",
            name=stmt.name,
            qualified_name=qualified,
            parent_local=parent,
            line_start=_start_line(stmt),
            line_end=stmt.end_lineno or stmt.lineno,
            signature=_class_signature(stmt),
            docstring=clip_docstring(ast.get_docstring(stmt)),
            is_exported=self._is_exported(stmt.name, top_level=parent is None),
        )
        for base in stmt.bases:
            short, full = _target_name(base)
            if short:
                self.b.add_edge(
                    src_local=node.local_id,
                    type="inherits",
                    dst_name=short,
                    dst_full=full,
                    line=getattr(base, "lineno", stmt.lineno),
                )
        self._decorators(stmt, node.local_id)
        self._body(
            stmt.body,
            parent=node.local_id,
            owner=node.local_id,
            scope=f"{scope}{stmt.name}.",
            in_class=True,
        )

    def _decorators(self, stmt: ast.stmt, node_local: int) -> None:
        for decorator in getattr(stmt, "decorator_list", []):
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            short, full = _target_name(target)
            if short:
                self.b.add_edge(
                    src_local=node_local,
                    type="decorates",
                    dst_name=short,
                    dst_full=full,
                    line=getattr(decorator, "lineno", None),
                )

    def _module_constant(self, stmt: ast.stmt) -> None:
        targets: list[ast.expr]
        value: ast.expr | None
        annotation: ast.expr | None = None
        if isinstance(stmt, ast.Assign):
            targets, value = list(stmt.targets), stmt.value
        elif isinstance(stmt, ast.AnnAssign):
            targets, value, annotation = [stmt.target], stmt.value, stmt.annotation
        else:
            return

        for target in targets:
            if not isinstance(target, ast.Name) or not _is_constant_name(target.id):
                continue
            signature = target.id
            if annotation is not None:
                signature += f": {ast.unparse(annotation)}"
            if value is not None:
                signature += f" = {_preview(value)}"
            self.b.add_node(
                type="variable",
                name=target.id,
                qualified_name=f"{self.rel_path}::{target.id}",
                parent_local=self.module_local,
                line_start=stmt.lineno,
                line_end=stmt.end_lineno or stmt.lineno,
                signature=signature,
                is_exported=self._is_exported(target.id, top_level=True),
            )

    def _import(self, stmt: ast.Import | ast.ImportFrom) -> None:
        modules: list[str] = []
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                self.b.add_import(
                    module=alias.name,
                    symbol=None,
                    alias=alias.asname,
                    line=stmt.lineno,
                    level=0,
                    is_relative=False,
                    is_reexport=self.is_package_init,
                )
                modules.append(alias.name)
        else:
            raw = "." * stmt.level + (stmt.module or "")
            for alias in stmt.names:
                self.b.add_import(
                    module=stmt.module or "",
                    symbol=alias.name,
                    alias=alias.asname,
                    line=stmt.lineno,
                    level=stmt.level,
                    is_relative=stmt.level > 0,
                    is_reexport=self.is_package_init
                    or (self.exported is not None and alias.name in self.exported),
                )
            modules.append(raw)

        for module in dict.fromkeys(modules):
            self.b.add_edge(
                src_local=self.module_local,
                type="imports",
                dst_name=module,
                dst_full=module,
                line=stmt.lineno,
            )

    # ----------------------------------------------------------------- calls

    def _calls_in(self, subtree: ast.AST, owner: int) -> None:
        """Record every call in ``subtree``, not descending into nested scopes."""
        for call in _iter_calls(subtree):
            short, full = _target_name(call.func)
            if short:
                self.b.add_edge(
                    src_local=owner,
                    type="calls",
                    dst_name=short,
                    dst_full=full,
                    line=call.lineno,
                )

    def _is_exported(self, name: str, *, top_level: bool) -> bool:
        if self.exported is not None and top_level:
            return name in self.exported
        return not name.startswith("_")


def _iter_calls(subtree: ast.AST) -> list[ast.Call]:
    """All ``Call`` nodes under ``subtree``, stopping at nested declarations."""
    found: list[ast.Call] = []
    if isinstance(subtree, ast.Call):
        found.append(subtree)
    stack: list[ast.AST] = [subtree]
    while stack:
        current = stack.pop()
        for child in ast.iter_child_nodes(current):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                continue
            if isinstance(child, ast.Call):
                found.append(child)
            stack.append(child)
    return found


def _target_name(expr: ast.expr) -> tuple[str | None, str | None]:
    """Short name and full dotted chain for a call/base/decorator target."""
    if isinstance(expr, ast.Name):
        return expr.id, expr.id
    if isinstance(expr, ast.Attribute):
        return expr.attr, _dotted(expr)
    if isinstance(expr, ast.Subscript):  # e.g. Generic[T] as a base class
        return _target_name(expr.value)
    if isinstance(expr, ast.Call):  # e.g. factory()(...)
        return _target_name(expr.func)
    return None, None


def _dotted(expr: ast.expr) -> str | None:
    parts: list[str] = []
    current: ast.expr = expr
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    elif isinstance(current, ast.Call):
        short, _ = _target_name(current.func)
        parts.append(f"{short}()" if short else "()")
    else:
        parts.append("?")
    return ".".join(reversed(parts))


def _start_line(stmt: ast.stmt) -> int:
    """First line of the declaration, decorators included."""
    decorators = getattr(stmt, "decorator_list", [])
    if decorators:
        return min(getattr(d, "lineno", stmt.lineno) for d in decorators)
    return stmt.lineno


def _function_signature(stmt: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async def " if isinstance(stmt, ast.AsyncFunctionDef) else "def "
    try:
        args = ast.unparse(stmt.args)
    except Exception:  # pragma: no cover - defensive, unparse is total in practice
        args = "..."
    returns = ""
    if stmt.returns is not None:
        try:
            returns = f" -> {ast.unparse(stmt.returns)}"
        except Exception:  # pragma: no cover
            returns = ""
    return _one_line(f"{prefix}{stmt.name}({args}){returns}")


def _class_signature(stmt: ast.ClassDef) -> str:
    parts: list[str] = []
    for base in stmt.bases:
        try:
            parts.append(ast.unparse(base))
        except Exception:  # pragma: no cover
            continue
    for keyword in stmt.keywords:
        try:
            parts.append(f"{keyword.arg}={ast.unparse(keyword.value)}")
        except Exception:  # pragma: no cover
            continue
    suffix = f"({', '.join(parts)})" if parts else ""
    return _one_line(f"class {stmt.name}{suffix}")


def _preview(value: ast.expr) -> str:
    try:
        text = _one_line(ast.unparse(value))
    except Exception:  # pragma: no cover
        return "..."
    if len(text) > MAX_VALUE_PREVIEW:
        text = text[: MAX_VALUE_PREVIEW - 1] + "…"
    return text


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _is_constant_name(name: str) -> bool:
    return name.isupper() and any(char.isalpha() for char in name) and not name.startswith("__")


def _read_dunder_all(tree: ast.Module) -> set[str] | None:
    """``__all__`` contents, if the module declares one as a plain list/tuple."""
    for stmt in tree.body:
        targets = stmt.targets if isinstance(stmt, ast.Assign) else []
        if isinstance(stmt, ast.AnnAssign):
            targets = [stmt.target]
        if not any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
            continue
        value = stmt.value
        if isinstance(value, ast.List | ast.Tuple | ast.Set):
            names = {
                element.value
                for element in value.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            }
            if names:
                return names
    return None
