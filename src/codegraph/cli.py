"""Command line entry point.

Uses ``argparse`` from the stdlib -- no click dependency for four subcommands.
Every expected failure (missing config, bad config, no database, schema
mismatch) exits with a one-line message rather than a traceback.
"""

from __future__ import annotations

import argparse
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

    build = subparsers.add_parser("build", help="index the project (incremental by default)")
    build.add_argument("--config", type=Path, help="path to .codegraph.toml")
    build.add_argument("--full", action="store_true", help="rebuild from scratch")
    build.add_argument("--verbose", action="store_true", help="log every file")

    serve = subparsers.add_parser("serve", help="run the MCP server on stdio")
    serve.add_argument("--config", type=Path, help="path to .codegraph.toml")

    stats = subparsers.add_parser("stats", help="what is in the graph")
    stats.add_argument("--config", type=Path, help="path to .codegraph.toml")

    query = subparsers.add_parser("query", help="look a symbol up without an agent")
    query.add_argument("symbol")
    query.add_argument("--config", type=Path, help="path to .codegraph.toml")
    query.add_argument("--definition", action="store_true", help="show the full definition")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
        if args.command == "query":
            return _cmd_query(config, args.symbol, definition=args.definition)
    except SchemaVersionError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except FileNotFoundError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
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
