"""Plain data carriers shared between parsers, the DB layer and the resolver.

Parsers work per-file and do not know about database ids, so nodes and edges
reference each other by ``local_id`` -- the index of the node inside
``ParseResult.nodes``.  ``db.write_parse_result`` translates those into real
row ids on insert.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Node types.  'endpoint' is synthesised by the HTTP bridge pass, not by parsers.
NODE_TYPES = (
    "module",
    "class",
    "function",
    "method",
    "variable",
    "interface",
    "type_alias",
    "enum",
    "component",
    "endpoint",
)

# Edge types.  'handles' / 'calls_api' come from the HTTP bridge pass.
EDGE_TYPES = (
    "calls",
    "imports",
    "inherits",
    "decorates",
    "implements",
    "references",
    "handles",
    "calls_api",
)

MAX_DOCSTRING_CHARS = 500


@dataclass(slots=True)
class Node:
    """A declaration inside one file."""

    local_id: int
    type: str
    name: str
    qualified_name: str
    line_start: int
    line_end: int
    parent_local: int | None = None
    signature: str | None = None
    docstring: str | None = None
    is_exported: bool = True
    is_async: bool = False


@dataclass(slots=True)
class Edge:
    """A relation whose source is always a node of the file being parsed.

    ``dst_name`` is the short target name (``method`` for ``obj.method()``) and
    is always populated -- even an unresolved edge is useful, because the name
    gives an agent something to search for.  ``dst_full`` keeps the whole
    dotted/member chain when there was one, which the resolver uses to follow
    ``module.symbol`` references.
    """

    src_local: int
    type: str
    dst_name: str
    line: int | None = None
    dst_full: str | None = None


@dataclass(slots=True)
class Import:
    """One import binding, kept separately because the resolver needs structure.

    ``module`` is the raw module string as written (``app.models``, ``./task``,
    ``react``).  ``symbol`` is the name taken out of it, or ``"*"`` for star
    imports / ``None`` for whole-module imports.  ``alias`` is the local binding.
    """

    module: str
    symbol: str | None = None
    alias: str | None = None
    line: int | None = None
    level: int = 0  # python relative-import depth; 0 for absolute and for TS
    is_relative: bool = False
    is_reexport: bool = False

    @property
    def local_name(self) -> str | None:
        """Name this import binds in the importing file."""
        return self.alias or self.symbol


@dataclass(slots=True)
class ParseResult:
    """Everything one parser extracted from one file."""

    language: str
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    imports: list[Import] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class NodeBuilder:
    """Small helper so parsers do not hand-manage ``local_id`` counters."""

    def __init__(self) -> None:
        self.result = ParseResult(language="")

    def add_node(self, **kwargs: object) -> Node:
        node = Node(local_id=len(self.result.nodes), **kwargs)  # type: ignore[arg-type]
        self.result.nodes.append(node)
        return node

    def add_edge(self, **kwargs: object) -> Edge:
        edge = Edge(**kwargs)  # type: ignore[arg-type]
        self.result.edges.append(edge)
        return edge

    def add_import(self, **kwargs: object) -> Import:
        imp = Import(**kwargs)  # type: ignore[arg-type]
        self.result.imports.append(imp)
        return imp


def clip_docstring(text: str | None) -> str | None:
    """Trim a docstring/JSDoc to a size that is cheap to ship to an agent."""
    if not text:
        return None
    text = text.strip()
    if not text:
        return None
    if len(text) > MAX_DOCSTRING_CHARS:
        text = text[: MAX_DOCSTRING_CHARS - 1].rstrip() + "…"
    return text
