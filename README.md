# codegraph

A local code-graph indexer. It parses a project into SQLite — declarations,
calls, imports, inheritance, and the HTTP routes that join a frontend to its
backend — and serves that graph to AI coding agents over MCP.

The point is token economy. Instead of reading whole files to find out where
something is defined or who calls it, an agent asks the graph and gets three
lines back.

Fully offline and deterministic: no LLMs, no embeddings, no vector store, no
network calls, no graph database. Python's `ast` for Python, `tree-sitter` for
TypeScript and PHP, `sqlite3` for storage.

## Install

codegraph is a tool, not a project dependency — install it once, into the
Python on your PATH, and use it from every project:

```bash
python -m pip install -e /path/to/codegraph
```

Installing it into a project's virtualenv also works: `init` records the full
path to the executable that ran it, so an agent finds it either way. The cost
is a copy per project, and a `codegraph init --force` to refresh the paths
whenever you move or rebuild that venv. Requires Python 3.11+.

## Use

From the root of the project you want to index:

```bash
codegraph init
```

That detects the layout, writes `.codegraph.toml`, registers the MCP server
with your agents, approves it where an agent asks before loading one, and adds
what it generated to an existing `.gitignore`. It never overwrites anything
(`--force` to replace the config, and to refresh an entry whose paths went
stale) and prints what it guessed, so a wrong guess is easy to correct by
hand.

Pick which agents to register with — no flag means all of them:

```bash
codegraph init --claude          # .mcp.json + .claude/settings.local.json
codegraph init --codex           # .codex/config.toml only
codegraph init --claude --codex  # same as no flag
codegraph init --no-clients      # just the config
```

`--no-gitignore` leaves `.gitignore` alone.

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
codegraph instructions   # the block telling an agent this repo has a graph
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

The server speaks MCP over stdio, so any MCP client can run it. Every path
`codegraph init` writes is absolute — the executable that ran it, and the
config it just generated — because an agent launches the server with the
system PATH and knows nothing about the virtualenv codegraph lives in. A bare
`codegraph` there is not found, and the server dies before it can say why.

**Claude Code** (CLI, desktop app, IDE extensions) reads `.mcp.json` from the
project root. `codegraph init --claude` writes two files, and the second
matters as much as the first:

`.mcp.json` — the server:

```json
{
  "mcpServers": {
    "codegraph": {
      "type": "stdio",
      "command": "C:/project/venv/Scripts/codegraph.exe",
      "args": ["serve", "--config", "C:/project/.codegraph.toml"]
    }
  }
}
```

`.claude/settings.local.json` — permission to load it:

```json
{ "enabledMcpjsonServers": ["codegraph"] }
```

A server declared in `.mcp.json` stays dormant until you approve it, and an
unanswered approval prompt is indistinguishable from a working setup: no tools,
no error, the agent quietly reads files instead. `enabledMcpjsonServers` is
that approval written down. `"enableAllProjectMcpServers": true` is the blunter
form, and `init` leaves it alone if you already have it.

Servers are read at startup, so restart the agent after `init`. To see what it
picked up: `/mcp` in a session, or `claude mcp list` outside one. With the CLI
you can register by hand instead — note that everything stays absolute:

```bash
claude mcp add -s local codegraph -- /full/path/to/codegraph serve --config /full/path/to/.codegraph.toml
```

**Claude Desktop** — the chat app, a different client from Claude Code — has
its own config at `%APPDATA%\Claude\claude_desktop_config.json` on Windows
(`~/Library/Application Support/Claude/` on macOS), which the app opens from
Settings → Developer → Edit Config. `init` does not write this one:

```json
{
  "mcpServers": {
    "codegraph": {
      "command": "C:/project/venv/Scripts/codegraph.exe",
      "args": ["serve", "--config", "C:/project/.codegraph.toml"]
    }
  }
}
```

**Codex** gets a project-local `.codex/config.toml` from `codegraph init
--codex`:

```toml
[mcp_servers.codegraph]
enabled = true
command = 'C:\project\venv\Scripts\codegraph.exe'
args = [
    'serve',
    '--config',
    'C:\project\.codegraph.toml',
]
cwd = 'C:\project'
startup_timeout_sec = 30
```

`cwd` is pinned too, because Codex launches the server with no project
context at all. The block also pre-approves every tool the server exposes;
without that, Codex stops and asks before each one. The same block works in the
global `~/.codex/config.toml` if you would rather keep it there — give each
project a distinct table name (`[mcp_servers.codegraph_shop]`) since that file
is shared.

None of the generated client files can be shared: each names an executable on
one machine, and `.claude/settings.local.json` records one person's decision.
`init` adds them to `.gitignore` along with `.codegraph/`. A teammate runs
`codegraph init` themselves — it takes two seconds and gets their paths
right.

Rebuild the graph after substantial edits — it is a snapshot, not a live view.
A build records a fingerprint of codegraph's own sources, so upgrading or
editing the tool makes the next `codegraph build` rebuild from scratch on its
own; you never have to remember `--full`.
A `post-commit` and `post-merge` git hook running `codegraph build` keeps it
current for a couple of seconds per commit:

```bash
printf '#!/bin/sh\ncodegraph build >/dev/null 2>&1 || true\n' > .git/hooks/post-commit
chmod +x .git/hooks/post-commit
```

## Telling the agent the graph exists

A connected client already gets the server's own instructions and every tool's
"reach for me when..." docstring, so in principle nothing else is needed. What a
repo file adds is the part codegraph can measure and you should not have to
maintain by hand:

```bash
codegraph instructions                     # print the block
codegraph instructions --write AGENTS.md   # insert it between markers
```

Once a graph is built, the block argues its case with this project's own
numbers, not a claim:

| File | `Read` | `get_file_outline` |
| --- | --- | --- |
| `frontend/src/pages/Scheduler.tsx` | ~34k tokens | ~1.4k tokens |

`--write` without a filename picks the `AGENTS.md` or `CLAUDE.md` already in the
project. It never creates one — an agent file is someone's own writing — and a
second run replaces the block between `<!-- codegraph:start -->` and
`<!-- codegraph:end -->` instead of appending a second copy, so refreshing the
numbers after a big change is one command. Claude Code reads `CLAUDE.md` and not
`AGENTS.md`; if you write to `AGENTS.md` and no `CLAUDE.md` imports it with
`@AGENTS.md`, the command tells you the block will not be loaded.

Keep it short around the block: an agent file is read at the start of every
session, and everything in it competes for the same context as the work.

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
| `get_directory_outline(path)` | which file you want, before opening one |
| `get_change_impact(base=None)` | what you are editing and what it breaks |
| `get_imports(path)` | to map module boundaries and blast radius |
| `get_neighbors(name, depth=1)` | to get context around an unfamiliar symbol |
| `trace_endpoint(path_or_name)` | to follow one HTTP route end to end |

All of them open the database read-only, respect `server.max_results`, and
never return function bodies except `get_definition` on a short declaration.

`get_change_impact` reads the git working tree when it can — changed files, the
declarations inside the changed line ranges, and the callers of those. Git is
not a requirement: without a repository, without the binary, or where it cannot
be run, it compares the working tree against the hashes the graph already
stores and says which source it used. A missing git is a different answer, not
an error.

## What it extracts

**Python** (`ast`): modules, classes, functions and methods with full
annotated signatures, module-level `UPPER_CASE` constants, docstrings;
`calls`, `imports`, `inherits` and `decorates` edges.

**TypeScript / TSX** (`tree-sitter`): functions (declarations, named arrow
functions and function expressions), classes and methods, interfaces, type
aliases, enums, module-level constants, JSDoc; `export`-awareness; functions
returning JSX are typed `component`. Same edge kinds, plus `implements`.

**PHP** (`tree-sitter`, mixed HTML/PHP grammar so templates parse): functions,
classes, methods, interfaces, traits, enums, `const` and `define()` constants,
docblocks; visibility drives the export flag; `require`/`include` and `use`
become imports. A block-bodied closure passed as an argument gets a node named
after the call receiving it (`add_action(wp_head)`) — WordPress themes keep
most of their logic there, and without a node the block would be invisible and
every call inside it credited to a file thousands of lines long.

PHP resolution is exact where the language is: functions live in one global
namespace with no import mechanism, so a unique project-wide name *is* the
definition, and a bare name the project does not declare *must* come from
outside it — the runtime, WordPress core, a plugin. Both are facts, not
heuristics.

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

**The bridge** (optional) reconstructs links no language expresses. Pick the
pack with `[bridge] backend_framework`.

*wordpress* (`indexer/wordpress.py`) reads the theme's string-based wiring:
`add_action`/`add_filter` become `hook` nodes with a `handles` edge to the
callback — a named function or the closure written inline; `wp_ajax_*` is an
HTTP endpoint hiding in a hook name and becomes an `endpoint`;
`get_template_part('template-parts/hero')` becomes a `renders` edge to the file
it pulls in; `register_rest_route` becomes an endpoint too.

*fastapi* (`indexer/bridge.py`) reads FastAPI route
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
