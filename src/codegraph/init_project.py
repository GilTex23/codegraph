"""``codegraph init`` -- set a project up for indexing.

Everything here writes into the *indexed* project, which the indexer itself
never does. That is the point of a separate, explicitly invoked command: a
build must stay read-only and predictable, while setup is a deliberate act the
user asked for. Nothing is overwritten without ``--force``.

Detection is deliberately shallow and explainable -- it reports what it found
so a wrong guess is obvious and easy to correct by hand.
"""

from __future__ import annotations

import json
import shutil
import sys
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .config import CONFIG_FILENAME, DEFAULT_EXCLUDE
from .indexer.walker import compile_ignore, is_ignored, rel_posix

MAX_SCANNED_FILES = 20000
FASTAPI_MARKERS = ("from fastapi", "import fastapi", "APIRouter(")
WORDPRESS_MARKERS = ("add_action(", "add_filter(", "get_template_part(", "wp_enqueue_")
API_DIR_NAMES = ("api", "services", "client", "clients")

GITIGNORE_HEADER = "# codegraph index"

# Agent clients init can register the MCP server with.
CLIENTS = ("claude", "codex")

CODEX_DIR = ".codex"
CODEX_CONFIG = "config.toml"
MCP_JSON = ".mcp.json"
CODEX_TABLE = "mcp_servers.codegraph"
CODEX_STARTUP_TIMEOUT_SEC = 30


@dataclass(slots=True)
class Detected:
    """What the scan believes the project looks like."""

    python_roots: list[str] = field(default_factory=list)
    ts_roots: list[str] = field(default_factory=list)
    php_roots: list[str] = field(default_factory=list)
    fastapi: bool = False
    wordpress: bool = False
    frontend_api_dir: str | None = None
    python_files: int = 0
    ts_files: int = 0
    php_files: int = 0

    @property
    def languages(self) -> list[str]:
        languages = []
        if self.python_files:
            languages.append("python")
        if self.ts_files:
            languages.append("typescript")
        if self.php_files:
            languages.append("php")
        return languages or ["python", "typescript"]

    @property
    def include(self) -> list[str]:
        roots = [*self.python_roots, *self.ts_roots, *self.php_roots]
        return sorted(dict.fromkeys(roots)) or ["."]

    @property
    def framework(self) -> str | None:
        """The bridge pack that fits, if any: only one runs per project."""
        if self.wordpress:
            return "wordpress"
        if self.fastapi and self.frontend_api_dir:
            return "fastapi"
        return None


@dataclass(slots=True)
class ClientResult:
    """Outcome of registering the MCP server with one agent client."""

    name: str
    path: Path
    written: bool = False
    already_present: bool = False


@dataclass(slots=True)
class InitResult:
    config_path: Path
    config_written: bool
    detected: Detected
    gitignore_path: Path | None = None
    gitignore_added: list[str] = field(default_factory=list)
    clients: list[ClientResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def detect(root: Path) -> Detected:
    """Guess include directories, languages and whether the bridge applies."""
    patterns = compile_ignore(DEFAULT_EXCLUDE)
    found = Detected()
    python_paths: list[str] = []
    ts_paths: list[str] = []
    php_paths: list[str] = []
    scanned = 0

    for path in sorted(root.rglob("*")):
        if scanned >= MAX_SCANNED_FILES:
            break
        if not path.is_file():
            continue
        relative = rel_posix(path, root)
        if is_ignored(relative, patterns):
            continue
        suffix = path.suffix.lower()
        if suffix in (".py", ".pyi"):
            scanned += 1
            python_paths.append(relative)
            if not found.fastapi and _mentions_fastapi(path):
                found.fastapi = True
        elif suffix in (".ts", ".tsx"):
            scanned += 1
            ts_paths.append(relative)
        elif suffix == ".php":
            scanned += 1
            php_paths.append(relative)
            if not found.wordpress and _mentions(path, WORDPRESS_MARKERS):
                found.wordpress = True

    found.python_files = len(python_paths)
    found.ts_files = len(ts_paths)
    found.php_files = len(php_paths)
    found.python_roots = _source_roots(python_paths, root)
    found.ts_roots = _source_roots(ts_paths, root)
    found.php_roots = _source_roots(php_paths, root)
    found.frontend_api_dir = _api_dir(found.ts_roots, root)
    return found


def _mentions(path: Path, markers: tuple[str, ...]) -> bool:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            head = handle.read(4096)
    except OSError:
        return False
    return any(marker in head for marker in markers)


def _mentions_fastapi(path: Path) -> bool:
    return _mentions(path, FASTAPI_MARKERS)


def _source_roots(paths: list[str], root: Path) -> list[str]:
    """Shallowest directories that contain the sources, narrowed to ``src/``.

    ``frontend/src/pages/x.tsx`` yields ``frontend/src/`` rather than
    ``frontend/``, so build output and tooling next to it stay out.  The
    majority decides rather than every file: a frontend package almost always
    keeps a ``vite.config.ts`` beside ``src/``, and one such file should not
    widen the whole include.
    """
    if not paths:
        return []
    tops: dict[str, list[str]] = {}
    for relative in paths:
        top = relative.split("/", 1)[0] if "/" in relative else "."
        tops.setdefault(top, []).append(relative)

    roots: list[str] = []
    for top, members in sorted(tops.items()):
        if top == ".":
            return ["."]
        prefix = f"{top}/src/"
        inside = sum(1 for member in members if member.startswith(prefix))
        if (root / top / "src").is_dir() and inside * 2 > len(members):
            roots.append(prefix)
        else:
            roots.append(f"{top}/")
    return roots


def _api_dir(ts_roots: list[str], root: Path) -> str | None:
    """The frontend directory holding the HTTP client, if there is an obvious one."""
    for ts_root in ts_roots:
        base = ts_root.rstrip("/")
        for parent in (base, f"{base}/src"):
            for name in API_DIR_NAMES:
                if (root / parent / name).is_dir():
                    return f"{parent}/{name}/"
    return None


def render_config(detected: Detected) -> str:
    """The ``.codegraph.toml`` text for a detected layout."""
    include = "\n".join(f'    "{entry}",' for entry in detected.include)
    languages = ", ".join(f'"{name}"' for name in detected.languages)
    exclude = "\n".join(f'    "{pattern}",' for pattern in DEFAULT_EXCLUDE)

    framework = detected.framework
    bridge_enabled = "true" if framework else "false"
    # Only the fastapi pack reads frontend_api_dir; emitting it for wordpress
    # would be a dead setting inviting someone to wonder what it does.
    api_dir_line = ""
    if framework != "wordpress":
        api_dir = detected.frontend_api_dir or "frontend/src/api/"
        api_dir_line = f'\nfrontend_api_dir = "{api_dir}"'

    return f"""\
# Written by `codegraph init`. Edit freely -- nothing regenerates this.
[project]
root = "."
db_path = ".codegraph/graph.db"

[index]
include = [
{include}
]
languages = [{languages}]
exclude = [
{exclude}
]
max_file_size_kb = 512

[server]
max_results = 50
snippet_max_lines = 40

# Reconstructs the links the language cannot express -- HTTP routes for
# fastapi, hooks and template parts for wordpress.
[bridge]
enabled = {bridge_enabled}
backend_framework = "{framework or "fastapi"}"{api_dir_line}
"""


def init_project(
    root: Path,
    *,
    force: bool = False,
    update_gitignore: bool = True,
    clients: Sequence[str] | None = None,
) -> InitResult:
    """Write the config, ignore the index, and register the MCP server.

    ``clients`` selects which agents to register with; ``None`` means all of
    them, an empty sequence means none.
    """
    root = root.resolve()
    detected = detect(root)
    config_path = root / CONFIG_FILENAME
    wanted = list(CLIENTS) if clients is None else [c for c in CLIENTS if c in clients]

    result = InitResult(config_path=config_path, config_written=False, detected=detected)

    if config_path.exists() and not force:
        result.notes.append(
            f"{CONFIG_FILENAME} already exists; left untouched (--force to replace)"
        )
    else:
        config_path.write_text(render_config(detected), encoding="utf-8", newline="\n")
        result.config_written = True

    if "claude" in wanted:
        _apply_claude(root, result)
    if "codex" in wanted:
        _apply_codex(root, config_path, force, result)

    if update_gitignore:
        _apply_gitignore(root, _gitignore_entries(wanted), result)

    if detected.framework is None:
        if detected.fastapi:
            result.notes.append(
                "FastAPI found but no frontend API directory; set [bridge] frontend_api_dir by hand"
            )
        else:
            result.notes.append("no supported framework found; [bridge] left disabled")
    return result


def _gitignore_entries(clients: Sequence[str]) -> list[str]:
    """What this run produced that must not reach version control.

    ``.mcp.json`` is deliberately absent: it holds a relative config path
    precisely so a team can share it. ``.codex/config.toml`` cannot be shared --
    it pins absolute paths to one machine's interpreter and checkout.
    """
    entries = [".codegraph/"]
    if "codex" in clients:
        entries.append(f"{CODEX_DIR}/")
    return entries


# --------------------------------------------------------------------- ignore


def _apply_gitignore(root: Path, entries: Iterable[str], result: InitResult) -> None:
    """Add the generated paths to an existing .gitignore, never create one."""
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        listed = ", ".join(entries)
        result.notes.append(f"no .gitignore here; keep {listed} out of version control yourself")
        return

    result.gitignore_path = gitignore
    try:
        existing = gitignore.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        result.notes.append(f".gitignore could not be read: {error}")
        return

    present = {line.strip().rstrip("/") for line in existing.splitlines()}
    missing = [entry for entry in entries if entry.rstrip("/") not in present]
    if not missing:
        return

    separator = "" if existing.endswith("\n") or not existing else "\n"
    body = "\n".join(missing)
    # A later run adding one more entry should extend the section, not start a
    # second one with the same heading.
    heading = "" if GITIGNORE_HEADER in existing else f"{GITIGNORE_HEADER}\n"
    try:
        with open(gitignore, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{separator}\n{heading}{body}\n")
    except OSError as error:
        result.notes.append(f".gitignore could not be written: {error}")
        return
    result.gitignore_added = missing


# --------------------------------------------------------------------- clients


def _apply_claude(root: Path, result: InitResult) -> None:
    """Register the server in project-scoped .mcp.json, which Claude Code reads."""
    mcp_path = root / MCP_JSON
    client = ClientResult(name="claude", path=mcp_path)
    result.clients.append(client)

    # A relative config path keeps the file portable -- .mcp.json is usually
    # committed, and an absolute path would break for everyone else. Clients
    # launch project servers with the project root as the working directory; if
    # one does not, serve fails with a clear "config not found" instead of
    # silently walking up into some other project.
    entry = {"command": "codegraph", "args": ["serve", "--config", CONFIG_FILENAME]}

    if mcp_path.exists():
        try:
            data = json.loads(mcp_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            result.notes.append(f"{MCP_JSON} exists but could not be parsed ({error}); left alone")
            return
        servers = data.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            result.notes.append(f"{MCP_JSON} has an unexpected shape; left alone")
            return
        if "codegraph" in servers:
            client.already_present = True
            return
        servers["codegraph"] = entry
    else:
        data = {"mcpServers": {"codegraph": entry}}

    mcp_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")
    client.written = True


def _apply_codex(root: Path, config_path: Path, force: bool, result: InitResult) -> None:
    """Register the server in a project-local .codex/config.toml.

    Codex has no notion of a project root when it launches a server, so every
    path here is absolute and ``cwd`` is pinned -- which is also why the file
    is machine-specific and gets gitignored.
    """
    codex_path = root / CODEX_DIR / CODEX_CONFIG
    client = ClientResult(name="codex", path=codex_path)
    result.clients.append(client)

    block = render_codex_entry(codegraph_executable(), config_path, root)

    if not codex_path.exists():
        codex_path.parent.mkdir(parents=True, exist_ok=True)
        codex_path.write_text(block, encoding="utf-8", newline="\n")
        client.written = True
        return

    try:
        existing = codex_path.read_text(encoding="utf-8", errors="replace")
        already = "codegraph" in (tomllib.loads(existing).get("mcp_servers") or {})
    except OSError as error:
        result.notes.append(f"{CODEX_DIR}/{CODEX_CONFIG} could not be read: {error}")
        return
    except tomllib.TOMLDecodeError as error:
        result.notes.append(f"{CODEX_DIR}/{CODEX_CONFIG} is not valid TOML ({error}); left alone")
        return

    if already and not force:
        client.already_present = True
        return

    updated = _replace_codex_block(existing, block) if already else _append_block(existing, block)
    codex_path.write_text(updated, encoding="utf-8", newline="\n")
    client.written = True


def _append_block(existing: str, block: str) -> str:
    separator = "" if existing.endswith("\n") or not existing else "\n"
    return f"{existing}{separator}\n{block}"


def _replace_codex_block(existing: str, block: str) -> str:
    """Swap an existing ``[mcp_servers.codegraph]`` table for a fresh one.

    Only ``--force`` gets here, and only to refresh paths that a moved
    virtualenv or checkout has invalidated.
    """
    lines = existing.splitlines(keepends=True)
    start = next(
        (i for i, line in enumerate(lines) if line.strip() == "[" + CODEX_TABLE + "]"),
        None,
    )
    if start is None:
        return _append_block(existing, block)
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("["):
            end = index
            break
    return "".join(lines[:start]) + block + "".join(lines[end:])


def codegraph_executable() -> str:
    """Absolute path to this codegraph, for clients that do not share our PATH.

    A desktop app inherits the system PATH and knows nothing about the
    virtualenv codegraph was installed into, so a bare ``codegraph`` would not
    be found. The console script sits next to the interpreter running us.
    """
    scripts = Path(sys.executable).parent
    for name in ("codegraph.exe", "codegraph"):
        candidate = scripts / name
        if candidate.exists():
            return str(candidate)
    return shutil.which("codegraph") or "codegraph"


def render_codex_entry(executable: str, config_path: Path, root: Path) -> str:
    """The ``[mcp_servers.codegraph]`` table for a project-local Codex config."""
    return (
        f"[{CODEX_TABLE}]\n"
        f"enabled = true\n"
        f"command = {_toml_string(executable)}\n"
        f"args = [\n"
        f"    'serve',\n"
        f"    '--config',\n"
        f"    {_toml_string(str(config_path))},\n"
        f"]\n"
        f"cwd = {_toml_string(str(root))}\n"
        f"startup_timeout_sec = {CODEX_STARTUP_TIMEOUT_SEC}\n\n"
        f"[{CODEX_TABLE}.tools.get_definition]\n"
        f"approval_mode = \"approve\"\n\n"
        f"[{CODEX_TABLE}.tools.get_directory_outline]\n"
        f"approval_mode = \"approve\"\n\n"
        f"[{CODEX_TABLE}.tools.get_imports]\n"
        f"approval_mode = \"approve\"\n\n"
        f"[{CODEX_TABLE}.tools.get_callers]\n"
        f"approval_mode = \"approve\"\n\n"
        f"[{CODEX_TABLE}.tools.trace_endpoint]\n"
        f"approval_mode = \"approve\"\n\n"
        f"[{CODEX_TABLE}.tools.get_file_outline]\n"
        f"approval_mode = \"approve\"\n\n"
        f"[{CODEX_TABLE}.tools.search_symbol]\n"
        f"approval_mode = \"approve\"\n\n"
        f"[{CODEX_TABLE}.tools.get_domain_slice]\n"
        f"approval_mode = \"approve\"\n\n"
        f"[{CODEX_TABLE}.tools.get_change_impact]\n"
        f"approval_mode = \"approve\"\n\n"
        f"[{CODEX_TABLE}.tools.get_project_overview]\n"
        f"approval_mode = \"approve\"\n"
    )


def _toml_string(value: str) -> str:
    """Prefer a literal string so Windows backslashes survive verbatim."""
    if "'" in value or "\n" in value:
        return json.dumps(value)  # a valid TOML basic string, backslashes escaped
    return f"'{value}'"


# ---------------------------------------------------------------------- report


def render_report(result: InitResult) -> str:
    """Human-readable summary of what init did and what it guessed."""
    detected = result.detected
    lines = [
        f"detected: {detected.python_files} Python file(s), {detected.ts_files} TypeScript file(s)",
        f"include:  {', '.join(detected.include)}",
        f"bridge:   {detected.framework or 'not detected'}"
        + (f"  api dir: {detected.frontend_api_dir}" if detected.frontend_api_dir else ""),
        "",
        f"{'wrote' if result.config_written else 'kept'} {result.config_path.name}",
    ]
    for client in result.clients:
        where = client.path.relative_to(result.config_path.parent).as_posix()
        if client.written:
            lines.append(f"registered for {client.name} in {where}")
        elif client.already_present:
            lines.append(f"{where} already lists codegraph")
    if result.gitignore_added:
        lines.append(f"added {', '.join(result.gitignore_added)} to .gitignore")
    elif result.gitignore_path is not None:
        lines.append(".gitignore already covers the generated files")
    lines.extend(f"note: {note}" for note in result.notes)
    lines.append("")
    lines.append("next: codegraph build")
    return "\n".join(lines)
