"""Command line entry point.

Uses ``argparse`` from the stdlib -- no click dependency for four subcommands.
Every expected failure (missing config, bad config, no database, schema
mismatch) exits with a one-line message rather than a traceback.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .config import Config, ConfigError, load_config
from .db import Database, SchemaVersionError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codegraph",
        description="Index a codebase into a SQLite graph and serve it to AI agents over MCP.",
    )
    parser.add_argument("--version", action="version", version=f"codegraph {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser(
        "init", help="write .codegraph.toml, ignore the index, register the MCP server"
    )
    init.add_argument("path", nargs="?", type=Path, default=Path("."), help="project root")
    init.add_argument("--force", action="store_true", help="replace an existing config")
    init.add_argument("--no-gitignore", action="store_true", help="do not touch .gitignore")
    init.add_argument(
        "--claude",
        action="store_true",
        help="register for Claude Code in .mcp.json and approve it in .claude/",
    )
    init.add_argument(
        "--codex", action="store_true", help="register for Codex in .codex/config.toml"
    )
    init.add_argument(
        "--no-clients", action="store_true", help="do not register the server with any agent"
    )

    build = subparsers.add_parser("build", help="index the project (incremental by default)")
    build.add_argument("--config", type=Path, help="path to .codegraph.toml")
    build.add_argument("--full", action="store_true", help="rebuild from scratch")
    build.add_argument("--verbose", action="store_true", help="log every file")

    serve = subparsers.add_parser("serve", help="run the MCP server on stdio")
    serve.add_argument("--config", type=Path, help="path to .codegraph.toml")

    stats = subparsers.add_parser("stats", help="what is in the graph")
    stats.add_argument("--config", type=Path, help="path to .codegraph.toml")

    guidance = subparsers.add_parser(
        "instructions", help="the block telling an agent this repo has a graph"
    )
    guidance.add_argument("--config", type=Path, help="path to .codegraph.toml")
    guidance.add_argument(
        "--write",
        nargs="?",
        const="",
        metavar="FILE",
        help="insert it into an agent file (AGENTS.md, CLAUDE.md) instead of printing it",
    )

    query = subparsers.add_parser("query", help="look a symbol up without an agent")
    query.add_argument("symbol")
    query.add_argument("--config", type=Path, help="path to .codegraph.toml")
    query.add_argument("--definition", action="store_true", help="show the full definition")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # init runs before any config exists, so it does not load one.
    if args.command == "init":
        return _cmd_init(args)

    try:
        config = load_config(args.config)
    except ConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    try:
        if args.command == "build":
            return _cmd_build(config, full=args.full, verbose=args.verbose)
        if args.command == "serve":
            return _cmd_serve(config)
        if args.command == "stats":
            return _cmd_stats(config)
        if args.command == "instructions":
            return _cmd_instructions(config, args.write)
        if args.command == "query":
            return _cmd_query(config, args.symbol, definition=args.definition)
    except SchemaVersionError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except FileNotFoundError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except (sqlite3.OperationalError, PermissionError) as error:
        # Almost always another process holding the graph: an MCP server serving
        # this project keeps it open for the life of the agent session.
        print(
            f"error: cannot use {config.db_path}: {error}\n"
            f"       Something else is holding it -- an agent's MCP server, or "
            f"another codegraph. Close it and retry.",
            file=sys.stderr,
        )
        return 2
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    from .init_project import init_project, render_report

    root = Path(args.path).expanduser()
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2

    # No client flag means every client; naming one narrows it to those named.
    if args.no_clients:
        clients: list[str] | None = []
    elif args.claude or args.codex:
        clients = [name for name, on in (("claude", args.claude), ("codex", args.codex)) if on]
    else:
        clients = None

    result = init_project(
        root,
        force=args.force,
        update_gitignore=not args.no_gitignore,
        clients=clients,
    )
    print(render_report(result))
    return 0


def _cmd_instructions(config: Config, target: str | None) -> int:
    from .instructions import claude_reads, default_target, render, write_block

    block = render(config)
    if target is None:
        print(block, end="")
        return 0

    if target == "":
        path = default_target(config.root)
        if path is None:
            print(
                "error: no AGENTS.md or CLAUDE.md here\n"
                "       create one, or name a file: codegraph instructions --write FILE",
                file=sys.stderr,
            )
            return 2
    else:
        path = Path(target)
        if not path.is_absolute():
            path = config.root / path
        if not path.is_file():
            # An agent file is someone's own writing; codegraph adds to it, and
            # only where there is already something to add to.
            print(f"error: not a file: {path}", file=sys.stderr)
            return 2

    refreshed = write_block(path, block)
    print(f"{'refreshed' if refreshed else 'added'} the codegraph block in {path.name}")
    if not claude_reads(config.root, path):
        print(
            f"note: Claude Code reads CLAUDE.md, not {path.name}; "
            f"put a line `@{path.name}` in CLAUDE.md so it loads this"
        )
    return 0


def _cmd_build(config: Config, *, full: bool, verbose: bool) -> int:
    from .indexer import build

    where = config.source or "(defaults, no .codegraph.toml found)"
    print(f"indexing {config.root}  config: {where}")
    summary = build(config, full=full, verbose=verbose)
    print(summary.render())
    print(f"graph: {config.db_path}")
    return 0


def _cmd_serve(config: Config) -> int:
    from .server.mcp_server import serve

    if not Path(config.db_path).exists():
        print(
            f"error: no graph at {config.db_path}. Run 'codegraph build' first.",
            file=sys.stderr,
        )
        return 2
    serve(config)
    return 0


def _cmd_stats(config: Config) -> int:
    with Database.open_readonly(config.db_path) as db:
        counts = db.counts()
        print(f"database: {config.db_path}")
        print(f"files: {counts['files']}   nodes: {counts['nodes']}   edges: {counts['edges']}")
        resolved = counts["edges"] - counts["unresolved_edges"]
        external = counts["external_edges"]
        # Third-party symbols can never resolve, so the honest coverage figure
        # is against the edges that could have resolved at all.
        resolvable = counts["edges"] - external or 1
        print(
            f"resolved: {resolved}/{counts['edges'] - external} resolvable  "
            f"({100 * resolved / resolvable:.1f}%)   "
            f"third-party: {external}   unknown: {counts['unresolved_edges'] - external}"
        )

        # A single percentage hides the fact that half the call edges could
        # never resolve: `obj.method()` needs type inference, which is out of
        # scope by design. Split by call shape so the number means something.
        print("\ncall resolution by shape:")
        shapes = (
            ("foo()", "(e.dst_full IS NULL OR e.dst_full = e.dst_name)"),
            ("obj.foo()", "e.dst_full IS NOT NULL AND e.dst_full != e.dst_name"),
        )
        for label, predicate in shapes:
            row = db.conn.execute(
                "SELECT count(*) AS total, "
                "sum(e.resolved) AS resolved, "
                "sum(e.confidence = 'external') AS external "
                f"FROM edges e WHERE e.type = 'calls' AND ({predicate})"
            ).fetchone()
            total, done = row["total"] or 0, row["resolved"] or 0
            resolvable = total - (row["external"] or 0)
            share = f"{100 * done / resolvable:.1f}%" if resolvable else "n/a"
            print(f"  {label:11} {done:5}/{resolvable:5} resolvable  ({share})")

        print("\nnodes by type:")
        for row in db.conn.execute(
            "SELECT type, count(*) AS n FROM nodes GROUP BY type ORDER BY n DESC"
        ):
            print(f"  {row['type']:12} {row['n']}")

        print("\nedges by type:")
        for row in db.conn.execute(
            "SELECT type, count(*) AS n, sum(resolved) AS r FROM edges GROUP BY type "
            "ORDER BY n DESC"
        ):
            print(f"  {row['type']:12} {row['n']:6} ({row['r'] or 0} resolved)")

        print("\nlargest files by declaration count:")
        for row in db.conn.execute(
            "SELECT f.path, count(n.id) AS n FROM files f LEFT JOIN nodes n ON n.file_id = f.id "
            "GROUP BY f.id ORDER BY n DESC LIMIT 10"
        ):
            print(f"  {row['n']:5}  {row['path']}")

        print("\nmost used third-party symbols:")
        for row in db.conn.execute(
            "SELECT dst_name, count(*) AS n FROM edges WHERE confidence = 'external' "
            "AND dst_name IS NOT NULL GROUP BY dst_name ORDER BY n DESC LIMIT 10"
        ):
            print(f"  {row['n']:5}  {row['dst_name']}")

        print("\nmost common unknown targets (candidates for a resolver gap):")
        for row in db.conn.execute(
            "SELECT dst_name, count(*) AS n FROM edges WHERE resolved = 0 "
            "AND confidence != 'external' AND dst_name IS NOT NULL "
            "GROUP BY dst_name ORDER BY n DESC LIMIT 10"
        ):
            print(f"  {row['n']:5}  {row['dst_name']}")
    return 0


def _cmd_query(config: Config, symbol: str, *, definition: bool) -> int:
    from .server.tools import GraphTools

    tools = GraphTools(config)
    try:
        print(tools.get_definition(symbol) if definition else tools.search_symbol(symbol))
    finally:
        tools.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
