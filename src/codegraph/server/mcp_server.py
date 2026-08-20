"""MCP server exposing the graph over stdio.

Thin on purpose: every tool is a docstring plus a one-line delegation to
``GraphTools``.  The docstrings matter more than they look -- they are the only
thing the agent sees when deciding whether to query the graph or fall back to
reading files, so each one says *when* to reach for it.

Written against the ``mcp`` 2.x API (``mcp.server.MCPServer``); the 1.x
``FastMCP`` class no longer exists.
"""

from __future__ import annotations

from ..config import Config
from .tools import GraphTools

INSTRUCTIONS = """\
A pre-built graph of this codebase: declarations, calls, imports, inheritance,
and (when configured) the HTTP routes joining frontend to backend.

Prefer these tools over reading files. Typical flow:
  get_project_overview     -> orient yourself in an unfamiliar repo
  get_domain_slice("task") -> every layer touching one domain entity, in one call
  search_symbol / get_definition -> find and read one declaration
  get_callers / get_callees / get_neighbors -> follow the blast radius of a change
  get_directory_outline    -> which file do I want, before opening one
  get_file_outline         -> instead of reading a whole file
  get_change_impact        -> what am I editing, and what does it break
  trace_endpoint           -> follow one HTTP route end to end

The graph is a snapshot; re-run `codegraph build` after large edits.
"""


def build_server(config: Config):
    """Create the MCP server bound to a project's graph."""
    from mcp.server import MCPServer

    tools = GraphTools(config)
    server = MCPServer(
        name="codegraph",
        version="0.1.0",
        instructions=INSTRUCTIONS,
    )

    @server.tool()
    def search_symbol(query: str, type: str | None = None) -> str:
        """Full-text search for declarations by name, signature or docstring.

        Use this first when you know roughly what something is called. Returns
        name, kind, file:line and signature -- never function bodies. Optional
        `type` filters to one of: module, class, function, method, variable,
        interface, type_alias, enum, component, endpoint.
        """
        return tools.search_symbol(query, type)

    @server.tool()
    def get_definition(name: str) -> str:
        """Show one symbol's definition: signature, docstring, path, line range.

        Includes the full source only when the declaration is shorter than the
        configured snippet limit; otherwise it tells you which lines to read.
        Pass a `qualified_name` (`path/to/file.py::Class.method`) when a plain
        name is ambiguous.
        """
        return tools.get_definition(name)

    @server.tool()
    def get_callers(name: str) -> str:
        """List everything that calls `name`, with file and call-site line.

        Use before changing a signature or deleting code. Edges inferred from a
        globally unique name rather than an explicit import are tagged
        `[heuristic]` -- treat those as leads, not proof.
        """
        return tools.get_callers(name)

    @server.tool()
    def get_callees(name: str) -> str:
        """List what `name` calls, with resolved targets where known.

        Use to understand what a function does without reading its body.
        Unresolved entries still show the raw target name, which is a good
        search term.
        """
        return tools.get_callees(name)

    @server.tool()
    def get_file_outline(path: str) -> str:
        """Structure of one file: every declaration, nested, with line ranges.

        Use this **instead of reading the file** when you only need to know
        what is in it. Bodies are never included.
        """
        return tools.get_file_outline(path)

    @server.tool()
    def get_directory_outline(path: str) -> str:
        """What is in a directory: one line per file with its exported names.

        Use this to decide **which** file you want, before spending a
        `get_file_outline` on it. Coarser on purpose — names only, no
        signatures — so a folder of twenty files still costs a few hundred
        tokens.
        """
        return tools.get_directory_outline(path)

    @server.tool()
    def get_change_impact(base: str | None = None) -> str:
        """What you are editing right now, and who calls it.

        Use at the start of a task to see the blast radius of work in progress:
        changed files, the declarations inside the changed lines, and their
        callers. `base` optionally names a git ref to compare against (a branch
        or commit); by default it reports uncommitted work. Falls back to
        comparing the working tree against the graph when git is unavailable.
        """
        return tools.get_change_impact(base)

    @server.tool()
    def get_imports(path: str) -> str:
        """What a file imports and which files import it.

        Use to map module boundaries, find the real source of a re-exported
        symbol, or judge the blast radius of editing a module.
        """
        return tools.get_imports(path)

    @server.tool()
    def get_neighbors(name: str, depth: int = 1) -> str:
        """The subgraph around a symbol: callers, callees, imports, inheritance.

        Use when a symbol is unfamiliar and you want its context in one call.
        `depth` is capped at 2 and the output is hard-limited; prefer depth 1.
        """
        return tools.get_neighbors(name, depth)

    @server.tool()
    def get_project_overview() -> str:
        """Directory-level map: file counts plus the key exported symbols.

        Use at the very start of a task in an unfamiliar repository, before
        any searching.
        """
        return tools.get_project_overview()

    @server.tool()
    def get_domain_slice(domain: str) -> str:
        """Every layer touching one domain entity, grouped model/schema/api/service/frontend.

        The highest-value query on a layered codebase: one call replaces a
        dozen searches. Naming style and plurals are normalised, so
        "design request", `design_requests` and `DesignRequest` are the same
        domain. Returns file paths and signatures only.
        """
        return tools.get_domain_slice(domain)

    @server.tool()
    def trace_endpoint(path_or_name: str) -> str:
        """Follow one HTTP route: endpoint -> handler -> callees -> frontend callers.

        Accepts a URL path (`/tasks/{task_id}`), a fragment of one, or a
        handler name. If the HTTP bridge is disabled the backend half is still
        returned, marked as such.
        """
        return tools.trace_endpoint(path_or_name)

    return server


def serve(config: Config) -> None:
    """Run the MCP server on stdio until the client disconnects."""
    build_server(config).run("stdio")
