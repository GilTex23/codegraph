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
from dataclasses import dataclass, field
from pathlib import Path

from .config import CONFIG_FILENAME, DEFAULT_EXCLUDE
from .indexer.walker import compile_ignore, is_ignored, rel_posix

MAX_SCANNED_FILES = 20000
FASTAPI_MARKERS = ("from fastapi", "import fastapi", "APIRouter(")
API_DIR_NAMES = ("api", "services", "client", "clients")

GITIGNORE_HEADER = "# codegraph index"


@dataclass(slots=True)
class Detected:
    """What the scan believes the project looks like."""

    python_roots: list[str] = field(default_factory=list)
    ts_roots: list[str] = field(default_factory=list)
    fastapi: bool = False
    frontend_api_dir: str | None = None
    python_files: int = 0
    ts_files: int = 0

    @property
    def languages(self) -> list[str]:
        languages = []
        if self.python_files:
            languages.append("python")
        if self.ts_files:
            languages.append("typescript")
        return languages or ["python", "typescript"]

    @property
    def include(self) -> list[str]:
        roots = [*self.python_roots, *self.ts_roots]
        return sorted(dict.fromkeys(roots)) or ["."]


@dataclass(slots=True)
class InitResult:
    config_path: Path
    config_written: bool
    detected: Detected
    gitignore_path: Path | None = None
    gitignore_updated: bool = False
    mcp_path: Path | None = None
    mcp_written: bool = False
    notes: list[str] = field(default_factory=list)


def detect(root: Path) -> Detected:
    """Guess include directories, languages and whether the bridge applies."""
    patterns = compile_ignore(DEFAULT_EXCLUDE)
    found = Detected()
    python_paths: list[str] = []
    ts_paths: list[str] = []
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

    found.python_files = len(python_paths)
    found.ts_files = len(ts_paths)
    found.python_roots = _source_roots(python_paths, root)
    found.ts_roots = _source_roots(ts_paths, root)
    found.frontend_api_dir = _api_dir(found.ts_roots, root)
    return found


def _mentions_fastapi(path: Path) -> bool:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            head = handle.read(4096)
    except OSError:
        return False
    return any(marker in head for marker in FASTAPI_MARKERS)


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

    bridge_enabled = "true" if detected.fastapi and detected.frontend_api_dir else "false"
    api_dir = detected.frontend_api_dir or "frontend/src/api/"

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

# Links frontend API calls to the backend handlers that serve them.
[bridge]
enabled = {bridge_enabled}
backend_framework = "fastapi"
frontend_api_dir = "{api_dir}"
"""


def init_project(
    root: Path,
    *,
    force: bool = False,
    update_gitignore: bool = True,
    write_mcp: bool = True,
) -> InitResult:
    """Write the config, ignore the index, and register the MCP server."""
    root = root.resolve()
    detected = detect(root)
    config_path = root / CONFIG_FILENAME

    result = InitResult(config_path=config_path, config_written=False, detected=detected)

    if config_path.exists() and not force:
        result.notes.append(
            f"{CONFIG_FILENAME} already exists; left untouched (--force to replace)"
        )
    else:
        config_path.write_text(render_config(detected), encoding="utf-8", newline="\n")
        result.config_written = True

    if update_gitignore:
        _apply_gitignore(root, result)

    if write_mcp:
        _apply_mcp_json(root, result)

    if not detected.fastapi:
        result.notes.append("no FastAPI usage found; [bridge] left disabled")
    elif not detected.frontend_api_dir:
        result.notes.append(
            "FastAPI found but no frontend API directory; set [bridge] frontend_api_dir by hand"
        )
    return result


def _apply_gitignore(root: Path, result: InitResult) -> None:
    """Add the index directory to an existing .gitignore, never create one."""
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        result.notes.append(
            "no .gitignore here; remember to keep .codegraph/ out of version control"
        )
        return

    result.gitignore_path = gitignore
    try:
        existing = gitignore.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        result.notes.append(f".gitignore could not be read: {error}")
        return

    entries = {line.strip().rstrip("/") for line in existing.splitlines()}
    if ".codegraph" in entries:
        return

    separator = "" if existing.endswith("\n") or not existing else "\n"
    addition = f"{separator}\n{GITIGNORE_HEADER}\n.codegraph/\n"
    try:
        with open(gitignore, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(addition)
    except OSError as error:
        result.notes.append(f".gitignore could not be written: {error}")
        return
    result.gitignore_updated = True


def _apply_mcp_json(root: Path, result: InitResult) -> None:
    """Register the server in project-scoped .mcp.json, which Claude Code reads."""
    mcp_path = root / ".mcp.json"
    result.mcp_path = mcp_path
    # A relative config path keeps the file portable -- .mcp.json is usually
    # committed, and an absolute path would break for everyone else. Clients
    # launch project servers with the project root as the working directory; if
    # one does not, serve fails with a clear "config not found" instead of
    # silently walking up into some other project.
    entry = {
        "command": "codegraph",
        "args": ["serve", "--config", CONFIG_FILENAME],
    }

    if mcp_path.exists():
        try:
            data = json.loads(mcp_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            result.notes.append(f".mcp.json exists but could not be parsed ({error}); left alone")
            return
        servers = data.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            result.notes.append(".mcp.json has an unexpected shape; left alone")
            return
        if "codegraph" in servers:
            return
        servers["codegraph"] = entry
    else:
        data = {"mcpServers": {"codegraph": entry}}

    mcp_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")
    result.mcp_written = True


def render_report(result: InitResult) -> str:
    """Human-readable summary of what init did and what it guessed."""
    detected = result.detected
    lines = [
        f"detected: {detected.python_files} Python file(s), {detected.ts_files} TypeScript file(s)",
        f"include:  {', '.join(detected.include)}",
        f"bridge:   {'fastapi' if detected.fastapi else 'not detected'}"
        + (f"  api dir: {detected.frontend_api_dir}" if detected.frontend_api_dir else ""),
        "",
        f"{'wrote' if result.config_written else 'kept'} {result.config_path.name}",
    ]
    if result.gitignore_updated:
        lines.append("added .codegraph/ to .gitignore")
    elif result.gitignore_path is not None:
        lines.append(".gitignore already covers .codegraph/")
    if result.mcp_written:
        lines.append("registered the MCP server in .mcp.json (read by Claude Code)")
    elif result.mcp_path is not None and result.mcp_path.exists():
        lines.append(".mcp.json already lists codegraph")
    lines.extend(f"note: {note}" for note in result.notes)
    lines.append("")
    lines.append("next: codegraph build")
    return "\n".join(lines)
