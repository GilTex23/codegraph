"""TypeScript / TSX parser built on tree-sitter.

Two passes over the concrete syntax tree:

1. *Structure* -- create nodes for every named declaration and remember which
   tree-sitter node each one came from.
2. *Calls* -- one walk maintaining a stack of "current owner"; a call is
   attributed to the nearest enclosing **named** declaration, so calls inside
   anonymous callbacks (``useEffect(() => fetchTasks())``) still land on the
   component instead of being dropped.

tree-sitter never raises on bad input: it produces ERROR nodes.  Those are
counted and reported rather than aborting the build.
"""

from __future__ import annotations

import functools

from tree_sitter import Language, Node, Parser, Tree

from ..models import NodeBuilder, ParseResult, clip_docstring

MAX_VALUE_PREVIEW = 80
MAX_SIGNATURE_CHARS = 200

JSX_TYPES = frozenset({"jsx_element", "jsx_self_closing_element", "jsx_fragment"})

FUNCTION_VALUE_TYPES = frozenset({"arrow_function", "function_expression"})

_CLASS_TYPES = frozenset({"class_declaration", "abstract_class_declaration", "class"})
_FUNCTION_DECL_TYPES = frozenset(
    {"function_declaration", "generator_function_declaration", "function_signature"}
)


@functools.lru_cache(maxsize=2)
def _parser_for(kind: str) -> Parser:
    """Build (once) a parser for ``typescript`` or ``tsx``."""
    import tree_sitter_typescript as tst

    raw = tst.language_tsx() if kind == "tsx" else tst.language_typescript()
    return Parser(Language(raw))


MAX_REPAIRS = 30


def parse_tree(kind: str, data: bytes) -> tuple[Tree, bytes]:
    """Parse, repairing the source when the grammar trips over valid code.

    ``tree-sitter-typescript`` cannot apply TypeScript's automatic semicolon
    insertion inside object types, so a member list written without ``;``
    breaks whenever a member name starts with a keyword the type grammar also
    uses as an operator::

        type T = {
          total_hours: number
          in_progress: number   // parsed as `number in ...`, everything after
        }                       // this point is lost

    ``in_progress`` is a common enough name that losing those files matters.
    When a parse fails we insert a semicolon at the end of the line before the
    error and try again, keeping the result only while the error count drops.
    Semicolons never add lines, so reported line numbers stay true to the file
    on disk; the returned buffer is the repaired one, so byte offsets used for
    names and signatures stay consistent with the tree.
    """
    parser = _parser_for(kind)
    tree = parser.parse(data)
    if not tree.root_node.has_error:
        return tree, data

    score = _repair_score(tree)
    for _ in range(MAX_REPAIRS):
        best: tuple[tuple[int, int], Tree, bytes] | None = None
        for candidate in _repair_candidates(data, tree):
            candidate_tree = parser.parse(candidate)
            candidate_score = _repair_score(candidate_tree)
            if best is None or candidate_score < best[0]:
                best = (candidate_score, candidate_tree, candidate)
        # Progress means fewer errors, or the same count further down the file:
        # repairing one member often just uncovers the next one.
        if best is None or best[0] >= score:
            break
        score, tree, data = best
        if score[0] == 0:
            break
    return tree, data


def parse_typescript(rel_path: str, source: str, language: str = "typescript") -> ParseResult:
    """Parse one .ts/.tsx file into nodes, edges and import records."""
    kind = "tsx" if language == "tsx" else "typescript"
    builder = NodeBuilder()
    builder.result.language = language

    tree, data = parse_tree(kind, source.encode("utf-8"))
    extractor = _Extractor(builder, rel_path, data, is_tsx=kind == "tsx")
    extractor.run(tree.root_node)
    return builder.result


def _repair_score(tree: Tree) -> tuple[int, int]:
    """Lower is better: fewer errors, then a first error further down the file."""
    row = _first_error_row(tree)
    return (_error_count(tree), -1 if row is None else -row)


def _error_count(tree: Tree) -> int:
    count = 0
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "ERROR" or node.is_missing:
            count += 1
            continue
        if node.has_error:
            stack.extend(node.children)
    return count


def _first_error_row(tree: Tree) -> int | None:
    stack = [tree.root_node]
    rows: list[int] = []
    while stack:
        node = stack.pop()
        if node.type == "ERROR" or node.is_missing:
            rows.append(node.start_point[0])
            continue
        if node.has_error:
            stack.extend(node.children)
    return min(rows) if rows else None


def _repair_candidates(data: bytes, tree: Tree) -> list[bytes]:
    """Buffers with one semicolon added near the first error.

    The unterminated member is usually the line the error starts on -- the
    grammar only notices the problem once it reaches the next name -- but it
    can also be the line above, so both are offered and the caller keeps
    whichever parses better.
    """
    row = _first_error_row(tree)
    if row is None:
        return []
    lines = data.split(b"\n")
    rows: list[int] = []
    if 0 <= row < len(lines):
        rows.append(row)
    above = row - 1
    while above >= 0 and not lines[above].strip():
        above -= 1
    if above >= 0:
        rows.append(above)

    candidates: list[bytes] = []
    for index in rows:
        stripped = lines[index].rstrip()
        if not stripped or stripped[-1:] in (b";", b",", b"{", b"(", b"["):
            continue
        patched = list(lines)
        patched[index] = stripped + b";"
        candidates.append(b"\n".join(patched))
    return candidates


class _Extractor:
    def __init__(self, builder: NodeBuilder, rel_path: str, data: bytes, *, is_tsx: bool) -> None:
        self.b = builder
        self.rel_path = rel_path
        self.data = data
        self.is_tsx = is_tsx
        self.claimed: dict[int, int] = {}  # tree-sitter node id -> our local_id
        self.local_exports: set[str] = set()
        self.by_name: dict[str, list[int]] = {}

    # ------------------------------------------------------------------- run

    def run(self, root: Node) -> None:
        module = self.b.add_node(
            type="module",
            name=self.rel_path.rsplit("/", 1)[-1].rsplit(".", 1)[0],
            qualified_name=self.rel_path,
            line_start=1,
            line_end=root.end_point[0] + 1,
        )
        self.module_local = module.local_id

        self._structure(root, parent=None, scope="", exported=False)
        self._calls(root, owner=module.local_id)
        self._apply_local_exports()
        self._count_errors(root)

    # ------------------------------------------------------- pass 1: structure

    def _structure(self, node: Node, *, parent: int | None, scope: str, exported: bool) -> None:
        for child in node.named_children:
            self._visit(child, parent=parent, scope=scope, exported=exported)

    def _visit(self, node: Node, *, parent: int | None, scope: str, exported: bool) -> None:
        kind = node.type

        if kind == "import_statement":
            self._import_statement(node)
            return
        if kind == "export_statement":
            self._export_statement(node, parent=parent, scope=scope)
            return
        if kind in _FUNCTION_DECL_TYPES:
            self._function_declaration(node, parent=parent, scope=scope, exported=exported)
            return
        if kind in _CLASS_TYPES:
            self._class_declaration(node, parent=parent, scope=scope, exported=exported)
            return
        if kind == "interface_declaration":
            self._simple_type(node, "interface", parent=parent, scope=scope, exported=exported)
            return
        if kind == "type_alias_declaration":
            self._simple_type(node, "type_alias", parent=parent, scope=scope, exported=exported)
            return
        if kind == "enum_declaration":
            self._simple_type(node, "enum", parent=parent, scope=scope, exported=exported)
            return
        if kind in ("lexical_declaration", "variable_declaration"):
            for declarator in node.named_children:
                if declarator.type == "variable_declarator":
                    self._declarator(
                        declarator, node, parent=parent, scope=scope, exported=exported
                    )
            return

        # Anything else: keep looking for declarations further down, but the
        # export flag does not propagate past a statement boundary.
        self._structure(node, parent=parent, scope=scope, exported=False)

    def _export_statement(self, node: Node, *, parent: int | None, scope: str) -> None:
        source = node.child_by_field_name("source")
        if source is not None:
            self._reexport(node, source)
            return

        declaration = node.child_by_field_name("declaration")
        if declaration is not None:
            self._visit(declaration, parent=parent, scope=scope, exported=True)
            return

        clause = next((c for c in node.named_children if c.type == "export_clause"), None)
        if clause is not None:
            for specifier in clause.named_children:
                name_node = specifier.child_by_field_name("name")
                if name_node is not None:
                    self.local_exports.add(self._text(name_node))
            return

        # `export default <expression>` where the expression is a function/class
        value = next((c for c in node.named_children if c.type != "comment"), None)
        if value is not None and value.type in FUNCTION_VALUE_TYPES:
            self._named_function_value(
                value,
                name="default",
                parent=parent,
                scope=scope,
                exported=True,
                decl_node=node,
            )

    # -------------------------------------------------------------- functions

    def _function_declaration(
        self, node: Node, *, parent: int | None, scope: str, exported: bool
    ) -> None:
        name_node = node.child_by_field_name("name")
        name = self._text(name_node) if name_node else "default"
        body = node.child_by_field_name("body")
        is_component = self.is_tsx and self._has_jsx(body)
        local = self._add(
            type="component" if is_component else "function",
            name=name,
            scope=scope,
            parent=parent,
            node=node,
            signature=self._function_signature(node, name, keyword="function"),
            docstring=self._jsdoc(node),
            exported=exported,
            is_async=self._is_async(node),
        )
        if body is not None:
            self._structure(body, parent=local, scope=f"{scope}{name}.", exported=False)

    def _declarator(
        self,
        declarator: Node,
        statement: Node,
        *,
        parent: int | None,
        scope: str,
        exported: bool,
    ) -> None:
        name_node = declarator.child_by_field_name("name")
        value = declarator.child_by_field_name("value")
        if name_node is None or name_node.type != "identifier":
            return
        name = self._text(name_node)

        if value is not None and value.type in FUNCTION_VALUE_TYPES:
            self._named_function_value(
                value,
                name=name,
                parent=parent,
                scope=scope,
                exported=exported,
                decl_node=statement,
            )
            return

        # Module-level constants are worth having in the graph; locals are noise.
        if parent is None and _is_constant_name(name):
            self._add(
                type="variable",
                name=name,
                scope=scope,
                parent=self.module_local,
                node=statement,
                signature=_clip(f"const {name} = {self._preview(value)}" if value else name),
                docstring=self._jsdoc(statement),
                exported=exported,
            )
        if value is not None:
            self._structure(value, parent=parent, scope=scope, exported=False)

    def _named_function_value(
        self,
        value: Node,
        *,
        name: str,
        parent: int | None,
        scope: str,
        exported: bool,
        decl_node: Node,
    ) -> None:
        body = value.child_by_field_name("body")
        is_component = self.is_tsx and self._has_jsx(body)
        local = self._add(
            type="component" if is_component else "function",
            name=name,
            scope=scope,
            parent=parent,
            node=decl_node,
            claim=value,
            signature=self._arrow_signature(value, name),
            docstring=self._jsdoc(decl_node),
            exported=exported,
            is_async=self._is_async(value),
        )
        if body is not None:
            self._structure(body, parent=local, scope=f"{scope}{name}.", exported=False)

    # ---------------------------------------------------------------- classes

    def _class_declaration(
        self, node: Node, *, parent: int | None, scope: str, exported: bool
    ) -> None:
        name_node = node.child_by_field_name("name")
        name = self._text(name_node) if name_node else "default"
        local = self._add(
            type="class",
            name=name,
            scope=scope,
            parent=parent,
            node=node,
            signature=self._class_signature(node, name),
            docstring=self._jsdoc(node),
            exported=exported,
        )
        self._heritage(node, local)

        body = node.child_by_field_name("body")
        if body is None:
            return
        member_scope = f"{scope}{name}."
        for member in body.named_children:
            if member.type == "method_definition":
                self._method(member, parent=local, scope=member_scope)
            elif member.type in ("public_field_definition", "field_definition"):
                value = member.child_by_field_name("value")
                member_name_node = member.child_by_field_name("name")
                if (
                    value is not None
                    and value.type in FUNCTION_VALUE_TYPES
                    and member_name_node is not None
                ):
                    self._method_from_field(
                        member, value, member_name_node, parent=local, scope=member_scope
                    )

    def _heritage(self, node: Node, local: int) -> None:
        heritage = next((c for c in node.named_children if c.type == "class_heritage"), None)
        if heritage is None:
            return
        for clause in heritage.named_children:
            if clause.type == "extends_clause":
                for target in clause.named_children:
                    if target.type == "type_arguments":
                        continue
                    short, full = _member_name(target, self.data)
                    if short:
                        self.b.add_edge(
                            src_local=local,
                            type="inherits",
                            dst_name=short,
                            dst_full=full,
                            line=target.start_point[0] + 1,
                        )
            elif clause.type == "implements_clause":
                for target in clause.named_children:
                    if target.type == "type_arguments":
                        continue
                    short, full = _member_name(target, self.data)
                    if short:
                        self.b.add_edge(
                            src_local=local,
                            type="implements",
                            dst_name=short,
                            dst_full=full,
                            line=target.start_point[0] + 1,
                        )

    def _method(self, node: Node, *, parent: int, scope: str) -> None:
        name_node = node.child_by_field_name("name")
        name = self._text(name_node) if name_node else "<anonymous>"
        local = self._add(
            type="method",
            name=name,
            scope=scope,
            parent=parent,
            node=node,
            signature=self._function_signature(node, name, keyword=""),
            docstring=self._jsdoc(node),
            exported=True,
            is_async=self._is_async(node),
        )
        body = node.child_by_field_name("body")
        if body is not None:
            self._structure(body, parent=local, scope=f"{scope}{name}.", exported=False)

    def _method_from_field(
        self, member: Node, value: Node, name_node: Node, *, parent: int, scope: str
    ) -> None:
        name = self._text(name_node)
        local = self._add(
            type="method",
            name=name,
            scope=scope,
            parent=parent,
            node=member,
            claim=value,
            signature=self._arrow_signature(value, name, keyword=""),
            docstring=self._jsdoc(member),
            exported=True,
            is_async=self._is_async(value),
        )
        body = value.child_by_field_name("body")
        if body is not None:
            self._structure(body, parent=local, scope=f"{scope}{name}.", exported=False)

    # ------------------------------------------------------ interfaces / types

    def _simple_type(
        self, node: Node, node_type: str, *, parent: int | None, scope: str, exported: bool
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self._text(name_node)
        self._add(
            type=node_type,
            name=name,
            scope=scope,
            parent=parent,
            node=node,
            signature=_clip(self._one_line(node)),
            docstring=self._jsdoc(node),
            exported=exported,
        )

    # ---------------------------------------------------------------- imports

    def _import_statement(self, node: Node) -> None:
        source = node.child_by_field_name("source")
        if source is None:
            return
        module = self._string_value(source)
        line = node.start_point[0] + 1
        clause = next((c for c in node.named_children if c.type == "import_clause"), None)

        if clause is None:
            self.b.add_import(
                module=module,
                symbol=None,
                alias=None,
                line=line,
                is_relative=module.startswith("."),
            )
        else:
            for part in clause.named_children:
                if part.type == "identifier":
                    self._add_import(module, "default", self._text(part), line)
                elif part.type == "namespace_import":
                    alias = next((c for c in part.named_children if c.type == "identifier"), None)
                    self._add_import(module, None, self._text(alias) if alias else None, line)
                elif part.type == "named_imports":
                    for specifier in part.named_children:
                        if specifier.type != "import_specifier":
                            continue
                        name_node = specifier.child_by_field_name("name")
                        alias_node = specifier.child_by_field_name("alias")
                        if name_node is None:
                            continue
                        self._add_import(
                            module,
                            self._text(name_node),
                            self._text(alias_node) if alias_node else None,
                            line,
                        )

        self.b.add_edge(
            src_local=self.module_local, type="imports", dst_name=module, dst_full=module, line=line
        )

    def _reexport(self, node: Node, source: Node) -> None:
        """``export { A } from './m'`` / ``export * from './m'``."""
        module = self._string_value(source)
        line = node.start_point[0] + 1
        clause = next((c for c in node.named_children if c.type == "export_clause"), None)
        namespace = next((c for c in node.named_children if c.type == "namespace_export"), None)

        if clause is not None:
            for specifier in clause.named_children:
                name_node = specifier.child_by_field_name("name")
                alias_node = specifier.child_by_field_name("alias")
                if name_node is None:
                    continue
                self._add_import(
                    module,
                    self._text(name_node),
                    self._text(alias_node) if alias_node else None,
                    line,
                    reexport=True,
                )
        elif namespace is not None:
            alias = next((c for c in namespace.named_children), None)
            self._add_import(module, "*", self._text(alias) if alias else None, line, reexport=True)
        else:
            self._add_import(module, "*", None, line, reexport=True)

        self.b.add_edge(
            src_local=self.module_local, type="imports", dst_name=module, dst_full=module, line=line
        )

    def _add_import(
        self,
        module: str,
        symbol: str | None,
        alias: str | None,
        line: int,
        *,
        reexport: bool = False,
    ) -> None:
        self.b.add_import(
            module=module,
            symbol=symbol,
            alias=alias,
            line=line,
            is_relative=module.startswith("."),
            is_reexport=reexport,
        )

    # ------------------------------------------------------------ pass 2: calls

    def _calls(self, node: Node, *, owner: int) -> None:
        stack: list[tuple[Node, int]] = [(node, owner)]
        while stack:
            current, current_owner = stack.pop()
            for child in current.named_children:
                child_owner = self.claimed.get(child.id, current_owner)
                if child.type == "call_expression":
                    self._record_call(child, child_owner, "function")
                elif child.type == "new_expression":
                    self._record_call(child, child_owner, "constructor")
                stack.append((child, child_owner))

    def _record_call(self, call: Node, owner: int, field: str) -> None:
        target = call.child_by_field_name(field)
        if target is None:
            return
        short, full = _member_name(target, self.data)
        if short:
            self.b.add_edge(
                src_local=owner,
                type="calls",
                dst_name=short,
                dst_full=full,
                line=call.start_point[0] + 1,
            )

    # ---------------------------------------------------------------- helpers

    def _add(
        self,
        *,
        type: str,
        name: str,
        scope: str,
        parent: int | None,
        node: Node,
        signature: str | None,
        docstring: str | None,
        exported: bool,
        is_async: bool = False,
        claim: Node | None = None,
    ) -> int:
        created = self.b.add_node(
            type=type,
            name=name,
            qualified_name=f"{self.rel_path}::{scope}{name}",
            parent_local=parent,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            signature=signature,
            docstring=docstring,
            is_exported=exported,
            is_async=is_async,
        )
        self.claimed[node.id] = created.local_id
        if claim is not None:
            self.claimed[claim.id] = created.local_id
        if parent is None or parent == self.module_local:
            self.by_name.setdefault(name, []).append(created.local_id)
        return created.local_id

    def _apply_local_exports(self) -> None:
        for name in self.local_exports:
            for local_id in self.by_name.get(name, []):
                self.b.result.nodes[local_id].is_exported = True

    def _count_errors(self, root: Node) -> None:
        if not root.has_error:
            return
        stack = [root]
        while stack:
            current = stack.pop()
            if current.type == "ERROR" or current.is_missing:
                self.b.result.errors.append(
                    f"{self.rel_path}:{current.start_point[0] + 1}: syntax error"
                )
                if len(self.b.result.errors) >= 5:
                    return
                continue
            if current.has_error:
                stack.extend(current.children)

    def _text(self, node: Node | None) -> str:
        if node is None:
            return ""
        return self.data[node.start_byte : node.end_byte].decode("utf-8", "replace")

    def _one_line(self, node: Node) -> str:
        return " ".join(self._text(node).split())

    def _string_value(self, node: Node) -> str:
        fragment = next((c for c in node.named_children if c.type == "string_fragment"), None)
        if fragment is not None:
            return self._text(fragment)
        return self._text(node).strip("'\"`")

    def _preview(self, node: Node | None) -> str:
        if node is None:
            return ""
        text = self._one_line(node)
        return text[: MAX_VALUE_PREVIEW - 1] + "…" if len(text) > MAX_VALUE_PREVIEW else text

    def _is_async(self, node: Node) -> bool:
        return any(child.type == "async" for child in node.children)

    def _function_signature(self, node: Node, name: str, *, keyword: str) -> str:
        params = self._one_line_field(node, "parameters")
        returns = self._one_line_field(node, "return_type")
        prefix = "async " if self._is_async(node) else ""
        head = f"{keyword} " if keyword else ""
        return _clip(f"{prefix}{head}{name}{params}{returns}")

    def _arrow_signature(self, value: Node, name: str, *, keyword: str = "const") -> str:
        params = self._one_line_field(value, "parameters")
        if not params:
            single = value.child_by_field_name("parameter")
            params = f"({self._one_line(single)})" if single else "()"
        returns = self._one_line_field(value, "return_type")
        prefix = "async " if self._is_async(value) else ""
        head = f"{keyword} " if keyword else ""
        return _clip(f"{head}{name} = {prefix}{params}{returns} =>")

    def _class_signature(self, node: Node, name: str) -> str:
        heritage = next((c for c in node.named_children if c.type == "class_heritage"), None)
        suffix = f" {self._one_line(heritage)}" if heritage else ""
        return _clip(f"class {name}{suffix}")

    def _one_line_field(self, node: Node, field: str) -> str:
        child = node.child_by_field_name(field)
        return self._one_line(child) if child is not None else ""

    def _has_jsx(self, body: Node | None) -> bool:
        if body is None:
            return False
        if body.type in JSX_TYPES:
            return True
        stack = [body]
        while stack:
            current = stack.pop()
            for child in current.named_children:
                if child.type in JSX_TYPES:
                    return True
                stack.append(child)
        return False

    def _jsdoc(self, node: Node) -> str | None:
        """The ``/** ... */`` comment directly above a declaration, if any.

        Also looks one level up, because ``export function f()`` puts the
        comment before the wrapping ``export_statement``.
        """
        candidates = [node]
        parent = node.parent
        if parent is not None and parent.type == "export_statement":
            candidates.append(parent)
        for candidate in candidates:
            sibling = candidate.prev_sibling
            while sibling is not None and not sibling.is_named and sibling.type != "comment":
                sibling = sibling.prev_sibling
            if sibling is None or sibling.type != "comment":
                continue
            if candidate.start_point[0] - sibling.end_point[0] > 1:
                continue
            raw = self._text(sibling)
            if raw.startswith("/**"):
                return clip_docstring(_strip_jsdoc(raw))
        return None


def _member_name(node: Node, data: bytes) -> tuple[str | None, str | None]:
    """Short name and full member chain for a callee / heritage target."""
    kind = node.type
    if kind in (
        "identifier",
        "type_identifier",
        "property_identifier",
        "shorthand_property_identifier",
    ):
        text = data[node.start_byte : node.end_byte].decode("utf-8", "replace")
        return text, text
    if kind == "member_expression":
        prop = node.child_by_field_name("property")
        short = data[prop.start_byte : prop.end_byte].decode("utf-8", "replace") if prop else None
        full = " ".join(data[node.start_byte : node.end_byte].decode("utf-8", "replace").split())
        return short, full
    if kind in (
        "generic_type",
        "nested_type_identifier",
        "non_null_expression",
        "parenthesized_expression",
        "as_expression",
        "await_expression",
    ):
        for child in node.named_children:
            short, full = _member_name(child, data)
            if short:
                return short, full
        return None, None
    if kind == "call_expression":
        target = node.child_by_field_name("function")
        return _member_name(target, data) if target else (None, None)
    return None, None


def _strip_jsdoc(raw: str) -> str:
    body = raw.removeprefix("/**").removesuffix("*/")
    lines = []
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("*"):
            line = line[1:].strip()
        lines.append(line)
    return "\n".join(lines).strip()


def _is_constant_name(name: str) -> bool:
    return name.isupper() and any(char.isalpha() for char in name)


def _clip(text: str) -> str:
    text = " ".join(text.split())
    if len(text) > MAX_SIGNATURE_CHARS:
        text = text[: MAX_SIGNATURE_CHARS - 1] + "…"
    return text
