# codegraph

A local code-graph indexer. It parses a project into SQLite — declarations,
calls, imports, inheritance, and the HTTP routes that join a frontend to its
backend — and serves that graph to AI coding agents over MCP.

The point is token economy. Instead of reading whole files to find out where
something is defined or who calls it, an agent asks the graph and gets three
lines back.

Fully offline and deterministic: no LLMs, no embeddings, no vector store, no
network calls, no graph database. Python's `ast` for Python, `tree-sitter` for
TypeScript, `sqlite3` for storage.

## Install

codegraph is a tool, not a project dependency — install it once, into the
Python on your PATH, and use it from every project:

```bash
python -m pip install -e /path/to/codegraph
```

Installing it into a project's virtualenv also works, but then the executable
only exists inside that venv, and desktop agent apps launching `codegraph`
will not find it. Requires Python 3.11+.

## Use

From the root of the project you want to index:

```bash
codegraph init
```

That detects the layout, writes `.codegraph.toml`, adds `.codegraph/` to an
existing `.gitignore`, and registers the MCP server in `.mcp.json`. It never
overwrites anything (`--force` to replace the config) and prints what it
guessed, so a wrong guess is easy to correct by hand. `--no-gitignore` and
`--no-mcp` opt out of the parts that touch other files.

Then build the graph:

```bash
codegraph build
```

That writes `.codegraph/graph.db`. Re-running it is incremental — unchanged
files are skipped by content hash. Use `--full` to rebuild from scratch.

```bash
codegraph init           # detect the layout and write the config
codegraph stats          # what ended up in the graph
codegraph query Task     # search without an agent, for debugging
codegraph serve          # MCP server on stdio
```

## Configuration

Put `.codegraph.toml` in the project root. Everything has a default, so the
file is optional — with no config at all, `codegraph build` indexes the current
directory with sensible exclusions.

```toml
[project]
root = "."
db_path = ".codegraph/graph.db"

[index]
include = ["backend/", "frontend/src/"]
languages = ["python", "typescript"]
exclude = [
    "**/node_modules/**", "**/.venv/**", "**/venv/**",
    "**/__pycache__/**", "**/dist/**", "**/build/**",
    "**/*.min.js", "**/*.d.ts", "**/migrations/**",
    "**/alembic/versions/**", "**/.git/**",
]
max_file_size_kb = 512

[server]
max_results = 50
snippet_max_lines = 40

# Optional: link frontend API calls to backend route handlers.
[bridge]
enabled = true
backend_framework = "fastapi"
frontend_api_dir = "frontend/src/api/"
```

## Connecting an agent

The server speaks MCP over stdio, so any MCP client can run it. Whichever
client you use, `codegraph` has to be findable: a GUI app inherits the system
PATH and knows nothing about your project's virtualenv, so install the tool
globally (see above) or spell out the full path to the executable.

**Claude Code** (CLI and desktop app) reads `.mcp.json` from the project root —
`codegraph init` writes it for you. To register it by hand instead:

```bash
claude mcp add codegraph -- codegraph serve --config .codegraph.toml
```

**Claude Desktop** reads `%APPDATA%\Claude\claude_desktop_config.json` on
Windows (`~/Library/Application Support/Claude/` on macOS); the app opens it
from Settings → Developer → Edit Config. It has no notion of a current project,
so the config path must be absolute:

```json
{
  "mcpServers": {
    "codegraph": {
      "command": "codegraph",
      "args": ["serve", "--config", "C:/path/to/project/.codegraph.toml"]
    }
  }
}
```

**Codex** reads `~/.codex/config.toml`:

```toml
[mcp_servers.codegraph]
command = "codegraph"
args = ["serve", "--config", "C:/path/to/project/.codegraph.toml"]
```

Add one block per project you want indexed, under distinct names
(`codegraph_shop`, `codegraph_admin`), since these configs are global.

Rebuild the graph after substantial edits — it is a snapshot, not a live view.
A `post-commit` and `post-merge` git hook running `codegraph build` keeps it
current for a couple of seconds per commit:

```bash
printf '#!/bin/sh\ncodegraph build >/dev/null 2>&1 || true\n' > .git/hooks/post-commit
chmod +x .git/hooks/post-commit
```

## MCP tools

| Tool | Use it when |
| --- | --- |
| `get_project_overview()` | starting a task in an unfamiliar repo |
| `get_domain_slice(domain)` | you need every layer touching one entity, in one call |
| `search_symbol(query, type=None)` | you know roughly what something is called |
| `get_definition(name)` | you need one declaration's signature, doc and source |
| `get_callers(name)` | before changing a signature or deleting code |
| `get_callees(name)` | to see what a function does without reading it |
| `get_file_outline(path)` | instead of reading a whole file |
| `get_imports(path)` | to map module boundaries and blast radius |
| `get_neighbors(name, depth=1)` | to get context around an unfamiliar symbol |
| `trace_endpoint(path_or_name)` | to follow one HTTP route end to end |

All of them open the database read-only, respect `server.max_results`, and
never return function bodies except `get_definition` on a short declaration.

## What it extracts

**Python** (`ast`): modules, classes, functions and methods with full
annotated signatures, module-level `UPPER_CASE` constants, docstrings;
`calls`, `imports`, `inherits` and `decorates` edges.

**TypeScript / TSX** (`tree-sitter`): functions (declarations, named arrow
functions and function expressions), classes and methods, interfaces, type
aliases, enums, module-level constants, JSDoc; `export`-awareness; functions
returning JSX are typed `component`. Same edge kinds, plus `implements`.

**Resolution** runs as a whole-project pass, in priority order: a declaration
in the same file (exact), an explicitly imported name (exact), a
project-globally unique name (heuristic), otherwise unresolved with the raw
target name kept. Re-export chains are followed to the real declaration, so
`from app.models import Task` lands on `app/models/task.py`, not on
`app/models/__init__.py` — and the same for TypeScript barrels
(`export * from './x'`). Depth is capped and cycles are guarded.

Three rules keep the graph honest rather than merely full:

- Calls to language builtins (`len`, `isinstance`, `parseInt`, `Date`, …) are
  dropped rather than stored as unresolved noise — they were a third of all
  call edges on real code and tell an agent nothing. A project that declares
  its own `len` keeps those edges, since the resolver checks project-wide names
  first.
- A name imported from a module outside the indexed tree (`useState` from
  `react`, `Depends` from `fastapi`) is recorded with `confidence = 'external'`.
  That is a third-party symbol, not a hole in the graph, and `codegraph stats`
  reports the two separately — otherwise the coverage figure looks broken when
  it isn't.
- The unique-name heuristic never crosses the Python/TypeScript boundary. A
  Python `db.add(...)` resolving to a TypeScript `add()` is worse than no edge
  at all, because nothing downstream can tell it is wrong.

### A grammar defect worth knowing about

`tree-sitter-typescript` does not apply TypeScript's automatic semicolon
insertion inside object types, so this valid code fails to parse — and
everything after it in the file is lost:

```ts
type Dashboard = {
  total_hours: number
  in_progress: number   // read as `number in ...`
}
```

Any member name starting with `in` triggers it, and `in_progress` is a very
common status name. Since 0.23.2 is the latest published grammar, codegraph
repairs it: on a failed parse it inserts a semicolon next to the error and
retries, keeping the result only while the parse actually improves. Semicolons
add no lines, so reported line numbers still match the file on disk. Genuinely
broken files still report an error and are not silently "fixed".

**The HTTP bridge** (optional, `indexer/bridge.py`) reads FastAPI route
decorators into `endpoint` nodes — folding in `APIRouter(prefix=…)` and
`include_router(…, prefix=…)` — and matches frontend request literals against
them. Both sides are normalised (`/tasks/{task_id}` and `/tasks/${id}` both
become `/tasks/:param`), so a page can be traced to the handler it hits.
Disable it with `enabled = false` and the whole pass is skipped.

## Cross-platform notes

Developed on Windows, intended to be identical on Linux:

- every path in the database is relative to `project.root` and uses `/`;
- files are read as UTF-8 explicitly, with newlines normalised, so a CRLF file
  and an LF file with the same content hash identically and report the same
  line numbers your editor shows;
- import resolution compares paths case-insensitively as a fallback, but the
  database keeps the original case.

## Notes on the schema

Two deliberate departures from the obvious design, both load-bearing:

- `edges.dst_id` is `ON DELETE SET NULL`, not `CASCADE`. With `CASCADE`,
  re-indexing file B would delete edges pointing into B from an unchanged file
  A, and A is never re-parsed — the graph would rot a little on every
  incremental build. Resolution is recomputed wholesale each run instead.
- There is an `imports` table alongside the `imports` *edges*. Edges cannot
  carry the module/symbol/alias/relative-level structure the resolver needs to
  follow re-export chains, and that structure has to survive builds in which
  the importing file is skipped.

Schema version lives in `PRAGMA user_version`; a mismatch asks for
`codegraph build --full` rather than failing obscurely.

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
pytest
ruff check src tests
```
