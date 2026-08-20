"""PHP parser built on tree-sitter.

Same two-pass shape as the TypeScript parser: build nodes for every named
declaration, then walk once attributing calls to the nearest enclosing
declaration.

Two things are specific to PHP and worth knowing:

* ``language_php`` is the mixed HTML/PHP grammar, which is what theme templates
  actually are -- markup with ``<?php ?>`` islands in it.
* A block-bodied anonymous function passed as an argument gets a node named
  after the call that receives it (``add_action(wp_head)``). WordPress themes
  put most of their logic in exactly those closures; without a node they would
  be invisible, and every call inside them would be attributed to a module that
  is often thousands of lines long.
"""

from __future__ import annotations

import functools

from tree_sitter import Language, Node, Parser, Tree

from ..models import NodeBuilder, ParseResult, clip_docstring

MAX_SIGNATURE_CHARS = 200
MAX_VALUE_PREVIEW = 80

# Anonymous functions with a real block body are worth a node; `fn($x) => $x*2`
# inline in a map is not.
_CLOSURE_TYPES = frozenset({"anonymous_function", "anonymous_function_creation_expression"})

_REQUIRE_TYPES = frozenset(
    {
        "require_expression",
        "require_once_expression",
        "include_expression",
        "include_once_expression",
    }
)

_CALL_TYPES = frozenset(
    {
        "function_call_expression",
        "member_call_expression",
        "scoped_call_expression",
        "nullsafe_member_call_expression",
        "object_creation_expression",
    }
)

_TYPE_DECLARATIONS: dict[str, str] = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "trait_declaration": "trait",
    "enum_declaration": "enum",
}

_NON_PUBLIC = frozenset({"private", "protected"})

# Modifiers are named nodes in this grammar, not bare keyword tokens.
_MODIFIER_TYPES = frozenset(
    {
        "visibility_modifier",
        "static_modifier",
        "abstract_modifier",
        "final_modifier",
        "readonly_modifier",
    }
)


@functools.lru_cache(maxsize=1)
def _parser() -> Parser:
    """Build (once) the mixed HTML/PHP parser used for templates and includes."""
    import tree_sitter_php as tsphp

    return Parser(Language(tsphp.language_php()))


def parse_php(rel_path: str, source: str) -> ParseResult:
    """Parse one .php file into nodes, edges and import records."""
    builder = NodeBuilder()
    builder.result.language = "php"
    data = source.encode("utf-8")
    tree: Tree = _parser().parse(data)
    _Extractor(builder, rel_path, data).run(tree.root_node)
    return builder.result


class _Extractor:
    def __init__(self, builder: NodeBuilder, rel_path: str, data: bytes) -> None:
        self.b = builder
        self.rel_path = rel_path
        self.data = data
        self.claimed: dict[int, int] = {}
        self.namespace = ""

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
        self._structure(root, parent=None, scope="")
        self._calls(root, owner=module.local_id)
        self._count_errors(root)

    # ------------------------------------------------------- pass 1: structure

    def _structure(self, node: Node, *, parent: int | None, scope: str) -> None:
        for child in node.named_children:
            self._visit(child, parent=parent, scope=scope)

    def _visit(self, node: Node, *, parent: int | None, scope: str) -> None:
        kind = node.type

        if kind == "namespace_definition":
            name = node.child_by_field_name("name")
            if name is not None:
                self.namespace = self._text(name)
            body = node.child_by_field_name("body")
            if body is not None:
                self._structure(body, parent=parent, scope=scope)
            return
        if kind == "namespace_use_declaration":
            self._use_declaration(node)
            return
        if kind in _REQUIRE_TYPES:
            self._require(node)
            return
        if kind == "function_definition":
            self._function(node, parent=parent, scope=scope)
            return
        if kind in _TYPE_DECLARATIONS:
            self._type_declaration(node, _TYPE_DECLARATIONS[kind], parent=parent, scope=scope)
            return
        if kind == "const_declaration":
            self._constants(node)
            return
        if kind == "function_call_expression":
            self._define_call(node)

        self._structure(node, parent=parent, scope=scope)

    # -------------------------------------------------------------- functions

    def _function(self, node: Node, *, parent: int | None, scope: str) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self._text(name_node)
        local = self._add(
            type="function",
            name=name,
            scope=scope,
            parent=parent,
            node=node,
            signature=self._function_signature(node, name),
            docstring=self._docblock(node),
            exported=True,  # PHP has no export list; a global function is public
        )
        body = node.child_by_field_name("body")
        if body is not None:
            self._structure(body, parent=local, scope=f"{scope}{name}.")

    def _method(self, node: Node, *, parent: int, scope: str) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self._text(name_node)
        modifiers = _modifiers(self.data, node)
        local = self._add(
            type="method",
            name=name,
            scope=scope,
            parent=parent,
            node=node,
            signature=self._function_signature(node, name),
            docstring=self._docblock(node),
            exported=not (modifiers & _NON_PUBLIC),
        )
        body = node.child_by_field_name("body")
        if body is not None:
            self._structure(body, parent=local, scope=f"{scope}{name}.")

    def _closure(self, closure: Node, call: Node, *, owner: int) -> int | None:
        """Name a callback after the call it is handed to.

        ``add_action('wp_head', function () { ... })`` becomes a node called
        ``add_action(wp_head)``: not a name anyone wrote, but the only handle
        the code offers, and far better than losing the block entirely.
        """
        body = closure.child_by_field_name("body")
        if body is None or body.type != "compound_statement":
            return None
        callee = call.child_by_field_name("function")
        called = self._text(callee) if callee is not None else "closure"
        hint = self._first_string_argument(call, before=closure)
        name = f"{called}({hint})" if hint else f"{called}@{closure.start_point[0] + 1}"
        local = self._add(
            type="function",
            name=name,
            scope="",
            parent=None,
            node=closure,
            signature=_clip(
                f"{called}({_quoted(hint)}function{self._one_line_field(closure, 'parameters')})"
            ),
            docstring=self._docblock(call.parent or call),
            exported=False,  # not callable by name; it exists only as a callback
        )
        self._structure(body, parent=local, scope=f"{name}.")
        return local

    # ------------------------------------------------------ classes and types

    def _type_declaration(
        self, node: Node, node_type: str, *, parent: int | None, scope: str
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self._text(name_node)
        local = self._add(
            type=node_type,
            name=name,
            scope=scope,
            parent=parent,
            node=node,
            signature=_clip(self._declaration_head(node, node_type, name)),
            docstring=self._docblock(node),
            exported=True,
        )
        self._heritage(node, local)

        body = node.child_by_field_name("body")
        if body is None:
            return
        member_scope = f"{scope}{name}."
        for member in body.named_children:
            if member.type == "method_declaration":
                self._method(member, parent=local, scope=member_scope)
            elif member.type == "const_declaration":
                self._constants(member, parent=local, scope=member_scope)

    def _heritage(self, node: Node, local: int) -> None:
        for child in node.named_children:
            if child.type == "base_clause":
                edge_type = "inherits"
            elif child.type == "class_interface_clause":
                edge_type = "implements"
            else:
                continue
            for target in child.named_children:
                name = self._text(target).rsplit("\\", 1)[-1]
                if name:
                    self.b.add_edge(
                        src_local=local,
                        type=edge_type,
                        dst_name=name,
                        dst_full=self._text(target),
                        line=target.start_point[0] + 1,
                    )

    # ------------------------------------------------------------- constants

    def _constants(self, node: Node, *, parent: int | None = None, scope: str = "") -> None:
        for element in node.named_children:
            if element.type != "const_element":
                continue
            name_node = next((c for c in element.named_children if c.type == "name"), None)
            if name_node is None:
                continue
            name = self._text(name_node)
            self._add(
                type="variable",
                name=name,
                scope=scope,
                parent=parent if parent is not None else self.module_local,
                node=node,
                signature=_clip(f"const {self._one_line(element)}"),
                docstring=self._docblock(node),
                exported=True,
            )

    def _define_call(self, node: Node) -> None:
        """``define('Z52_VERSION', '1.0.0')`` declares a constant just as much."""
        callee = node.child_by_field_name("function")
        if callee is None or self._text(callee) != "define":
            return
        name = self._first_string_argument(node)
        if not name:
            return
        self._add(
            type="variable",
            name=name,
            scope="",
            parent=self.module_local,
            node=node,
            signature=_clip(self._one_line(node)),
            docstring=None,
            exported=True,
        )

    # ---------------------------------------------------------------- imports

    def _use_declaration(self, node: Node) -> None:
        line = node.start_point[0] + 1
        prefix = next((c for c in node.named_children if c.type == "namespace_name"), None)
        group = next((c for c in node.named_children if c.type == "namespace_use_group"), None)

        if prefix is not None and group is not None:
            base = self._text(prefix)
            for clause in group.named_children:
                symbol = self._text(clause).rsplit("\\", 1)[-1]
                self._add_import(base, symbol, line)
            return

        for clause in node.named_children:
            if clause.type != "namespace_use_clause":
                continue
            full = self._text(clause).split(" as ")[0].strip()
            alias_node = next((c for c in clause.named_children if c.type == "name"), None)
            module, _, symbol = full.rpartition("\\")
            self._add_import(
                module or full,
                symbol or full,
                line,
                alias=self._text(alias_node)
                if alias_node and " as " in self._text(clause)
                else None,
            )

    def _add_import(
        self, module: str, symbol: str | None, line: int, alias: str | None = None
    ) -> None:
        self.b.add_import(module=module, symbol=symbol, alias=alias, line=line)
        self.b.add_edge(
            src_local=self.module_local,
            type="imports",
            dst_name=module,
            dst_full=module,
            line=line,
        )

    def _require(self, node: Node) -> None:
        """``require_once Z52_CHILD_DIR . '/inc/x.php'`` -- keep the literal part.

        The prefix is a constant or a function call whose value is unknowable
        here, so the string fragment is all there is; the resolver matches it
        against indexed paths by suffix.
        """
        literal = self._first_string_in(node)
        if not literal:
            return
        line = node.start_point[0] + 1
        self.b.add_import(module=literal, symbol=None, alias=None, line=line, is_relative=True)
        self.b.add_edge(
            src_local=self.module_local,
            type="imports",
            dst_name=literal,
            dst_full=self._one_line(node),
            line=line,
        )

    # ------------------------------------------------------------ pass 2: calls

    def _calls(self, root: Node, *, owner: int) -> None:
        stack: list[tuple[Node, int]] = [(root, owner)]
        while stack:
            current, current_owner = stack.pop()
            for child in current.named_children:
                child_owner = self.claimed.get(child.id, current_owner)
                if child.type in _CALL_TYPES:
                    self._record_call(child, child_owner)
                stack.append((child, child_owner))

    def _record_call(self, call: Node, owner: int) -> None:
        if call.type == "object_creation_expression":
            target = next((c for c in call.named_children if c.type != "arguments"), None)
            if target is None:
                return
            full = self._text(target)
            self.b.add_edge(
                src_local=owner,
                type="calls",
                dst_name=full.rsplit("\\", 1)[-1],
                dst_full=full,
                line=call.start_point[0] + 1,
            )
            return

        if call.type == "function_call_expression":
            callee = call.child_by_field_name("function")
            if callee is None:
                return
            full = self._one_line(callee)
            short = full.rsplit("\\", 1)[-1]
        else:  # member / scoped / nullsafe
            name_node = call.child_by_field_name("name")
            if name_node is None:
                return
            short = self._text(name_node)
            full = self._one_line(
                call.child_by_field_name("object") or call.child_by_field_name("scope") or name_node
            )
            full = f"{full}.{short}" if full and full != short else short

        if short and not short.startswith("$"):
            self.b.add_edge(
                src_local=owner,
                type="calls",
                dst_name=short,
                dst_full=full,
                line=call.start_point[0] + 1,
            )

        # A callback handed to this call becomes its own declaration.
        arguments = call.child_by_field_name("arguments")
        if arguments is not None:
            for argument in arguments.named_children:
                for candidate in (argument, *argument.named_children):
                    if candidate.type in _CLOSURE_TYPES and candidate.id not in self.claimed:
                        self._closure(candidate, call, owner=owner)

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
        )
        self.claimed[node.id] = created.local_id
        return created.local_id

    def _text(self, node: Node | None) -> str:
        if node is None:
            return ""
        return self.data[node.start_byte : node.end_byte].decode("utf-8", "replace")

    def _one_line(self, node: Node | None) -> str:
        return " ".join(self._text(node).split())

    def _one_line_field(self, node: Node, field: str) -> str:
        child = node.child_by_field_name(field)
        return self._one_line(child) if child is not None else "()"

    def _function_signature(self, node: Node, name: str) -> str:
        parameters = self._one_line_field(node, "parameters")
        returns = self._one_line_field(node, "return_type")
        returns = f": {returns}" if returns and returns != "()" else ""
        prefix = " ".join(sorted(_modifiers(self.data, node)))
        head = f"{prefix} " if prefix else ""
        return _clip(f"{head}function {name}{parameters}{returns}")

    def _declaration_head(self, node: Node, node_type: str, name: str) -> str:
        parts = [f"{node_type} {name}"]
        for child in node.named_children:
            if child.type in ("base_clause", "class_interface_clause"):
                parts.append(self._one_line(child))
        return " ".join(parts)

    def _first_string_argument(self, call: Node, before: Node | None = None) -> str | None:
        """First string literal among the arguments, stopping at ``before``.

        The search must not reach inside a callback: a closure's own body is
        full of strings, and one of them would otherwise end up naming the hook.
        """
        arguments = call.child_by_field_name("arguments")
        if arguments is None:
            return None
        for argument in arguments.named_children:
            if before is not None and _contains(argument, before):
                break
            if _contains_any(argument, _CLOSURE_TYPES):
                break
            found = self._first_string_in(argument)
            if found:
                return found
        return None

    def _first_string_in(self, node: Node) -> str | None:
        stack = [node]
        while stack:
            current = stack.pop(0)
            if current.type in ("string", "encapsed_string"):
                fragment = next(
                    (c for c in current.named_children if c.type == "string_content"), None
                )
                return self._text(fragment) if fragment is not None else ""
            stack.extend(current.named_children)
        return None

    def _docblock(self, node: Node) -> str | None:
        """The ``/** ... */`` immediately above a declaration."""
        sibling = node.prev_sibling
        while sibling is not None and not sibling.is_named and sibling.type != "comment":
            sibling = sibling.prev_sibling
        if sibling is None or sibling.type != "comment":
            return None
        if node.start_point[0] - sibling.end_point[0] > 1:
            return None
        raw = self._text(sibling)
        if not raw.startswith("/**"):
            return None
        return clip_docstring(_strip_docblock(raw))

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


def _quoted(hint: str | None) -> str:
    """The hook name as it appears in the source, ready to slot into a call."""
    return f"'{hint}', " if hint else ""


def _modifiers(data: bytes, node: Node) -> set[str]:
    return {
        data[child.start_byte : child.end_byte].decode("utf-8", "replace")
        for child in node.named_children
        if child.type in _MODIFIER_TYPES
    }


def _contains(node: Node, target: Node) -> bool:
    return node.start_byte <= target.start_byte and node.end_byte >= target.end_byte


def _contains_any(node: Node, kinds: frozenset[str]) -> bool:
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in kinds:
            return True
        stack.extend(current.named_children)
    return False


def _strip_docblock(raw: str) -> str:
    body = raw.removeprefix("/**").removesuffix("*/")
    lines = []
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("*"):
            line = line[1:].strip()
        lines.append(line)
    return "\n".join(lines).strip()


def _clip(text: str) -> str:
    text = " ".join(text.split())
    if len(text) > MAX_SIGNATURE_CHARS:
        text = text[: MAX_SIGNATURE_CHARS - 1] + "…"
    return text
