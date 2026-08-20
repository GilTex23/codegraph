"""Reading and validating ``.codegraph.toml``.

Every field has a default, so ``codegraph build`` works in a project with no
config at all.  Validation is explicit: a bad config produces a one-line
human-readable error, never a traceback.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_FILENAME = ".codegraph.toml"

DEFAULT_EXCLUDE: tuple[str, ...] = (
    "**/node_modules/**",
    "**/.venv/**",
    "**/venv/**",
    "**/__pycache__/**",
    "**/dist/**",
    "**/build/**",
    "**/*.min.js",
    "**/*.d.ts",
    "**/migrations/**",
    "**/alembic/versions/**",
    "**/.git/**",
    "**/.codegraph/**",
    "**/logs/**",
)

KNOWN_LANGUAGES = ("python", "typescript")
KNOWN_FRAMEWORKS = ("fastapi",)


class ConfigError(Exception):
    """Raised for anything wrong with the config file; printed as-is to the user."""


@dataclass(slots=True)
class ProjectConfig:
    root: Path
    db_path: Path


@dataclass(slots=True)
class IndexConfig:
    include: list[str] = field(default_factory=lambda: ["."])
    languages: list[str] = field(default_factory=lambda: list(KNOWN_LANGUAGES))
    exclude: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE))
    max_file_size_kb: int = 512


@dataclass(slots=True)
class ServerConfig:
    max_results: int = 50
    snippet_max_lines: int = 40


@dataclass(slots=True)
class BridgeConfig:
    enabled: bool = False
    backend_framework: str = "fastapi"
    frontend_api_dir: str | None = None


@dataclass(slots=True)
class Config:
    project: ProjectConfig
    index: IndexConfig = field(default_factory=IndexConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    bridge: BridgeConfig = field(default_factory=BridgeConfig)
    source: Path | None = None  # the .codegraph.toml this came from, if any

    @property
    def root(self) -> Path:
        return self.project.root

    @property
    def db_path(self) -> Path:
        return self.project.db_path


def find_config(start: Path | None = None) -> Path | None:
    """Walk up from ``start`` looking for ``.codegraph.toml``."""
    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        path = candidate / CONFIG_FILENAME
        if path.is_file():
            return path
    return None


def load_config(explicit_path: Path | None = None, cwd: Path | None = None) -> Config:
    """Load a config, falling back to defaults rooted at ``cwd``.

    ``explicit_path`` may point at the toml file itself or at a directory
    containing one.
    """
    cwd = (cwd or Path.cwd()).resolve()

    path: Path | None
    if explicit_path is not None:
        path = Path(explicit_path).expanduser()
        if path.is_dir():
            path = path / CONFIG_FILENAME
        if not path.is_file():
            raise ConfigError(f"Config file not found: {path}")
    else:
        path = find_config(cwd)

    if path is None:
        return Config(
            project=ProjectConfig(root=cwd, db_path=cwd / ".codegraph" / "graph.db"),
            source=None,
        )

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from None
    except OSError as exc:
        raise ConfigError(f"{path}: cannot be read: {exc}") from None

    return _build(raw, path)


def _build(raw: dict, path: Path) -> Config:
    where = path.name
    _reject_unknown(raw, {"project", "index", "server", "bridge"}, where, "top level")

    base = path.parent

    project_raw = _section(raw, "project", where)
    _reject_unknown(project_raw, {"root", "db_path"}, where, "[project]")
    root_str = _str(project_raw, "root", ".", where, "[project]")
    root = Path(root_str) if Path(root_str).is_absolute() else (base / root_str)
    root = root.resolve()
    if not root.is_dir():
        raise ConfigError(f"{where}: [project] root points at a missing directory: {root}")

    db_str = _str(project_raw, "db_path", ".codegraph/graph.db", where, "[project]")
    db_path = Path(db_str) if Path(db_str).is_absolute() else root / db_str

    index_raw = _section(raw, "index", where)
    _reject_unknown(
        index_raw,
        {"include", "languages", "exclude", "max_file_size_kb"},
        where,
        "[index]",
    )
    include = _str_list(index_raw, "include", ["."], where, "[index]")
    languages = _str_list(index_raw, "languages", list(KNOWN_LANGUAGES), where, "[index]")
    for lang in languages:
        if lang not in KNOWN_LANGUAGES:
            raise ConfigError(
                f"{where}: [index] unknown language {lang!r}; "
                f"supported: {', '.join(KNOWN_LANGUAGES)}"
            )
    exclude = _str_list(index_raw, "exclude", list(DEFAULT_EXCLUDE), where, "[index]")
    max_kb = _int(index_raw, "max_file_size_kb", 512, where, "[index]")
    if max_kb <= 0:
        raise ConfigError(f"{where}: [index] max_file_size_kb must be positive, got {max_kb}")

    server_raw = _section(raw, "server", where)
    _reject_unknown(server_raw, {"max_results", "snippet_max_lines"}, where, "[server]")
    max_results = _int(server_raw, "max_results", 50, where, "[server]")
    snippet_max_lines = _int(server_raw, "snippet_max_lines", 40, where, "[server]")
    if max_results <= 0:
        raise ConfigError(f"{where}: [server] max_results must be positive, got {max_results}")
    if snippet_max_lines <= 0:
        raise ConfigError(
            f"{where}: [server] snippet_max_lines must be positive, got {snippet_max_lines}"
        )

    bridge_raw = _section(raw, "bridge", where)
    _reject_unknown(
        bridge_raw,
        {"enabled", "backend_framework", "frontend_api_dir"},
        where,
        "[bridge]",
    )
    enabled = _bool(bridge_raw, "enabled", False, where, "[bridge]")
    framework = _str(bridge_raw, "backend_framework", "fastapi", where, "[bridge]")
    if enabled and framework not in KNOWN_FRAMEWORKS:
        raise ConfigError(
            f"{where}: [bridge] backend_framework {framework!r} is not supported; "
            f"supported: {', '.join(KNOWN_FRAMEWORKS)}"
        )
    frontend_api_dir = bridge_raw.get("frontend_api_dir")
    if frontend_api_dir is not None and not isinstance(frontend_api_dir, str):
        raise ConfigError(f"{where}: [bridge] frontend_api_dir must be a string")

    return Config(
        project=ProjectConfig(root=root, db_path=db_path),
        index=IndexConfig(
            include=include,
            languages=languages,
            exclude=exclude,
            max_file_size_kb=max_kb,
        ),
        server=ServerConfig(max_results=max_results, snippet_max_lines=snippet_max_lines),
        bridge=BridgeConfig(
            enabled=enabled,
            backend_framework=framework,
            frontend_api_dir=frontend_api_dir,
        ),
        source=path,
    )


def _section(raw: dict, name: str, where: str) -> dict:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: [{name}] must be a table")
    return value


def _reject_unknown(raw: dict, allowed: set[str], where: str, section: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(
            f"{where}: unknown key(s) in {section}: {', '.join(unknown)}. "
            f"Allowed: {', '.join(sorted(allowed))}"
        )


def _str(raw: dict, key: str, default: str, where: str, section: str) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"{where}: {section} {key} must be a string, got {type(value).__name__}")
    return value


def _int(raw: dict, key: str, default: int, where: str, section: str) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}: {section} {key} must be an integer")
    return value


def _bool(raw: dict, key: str, default: bool, where: str, section: str) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: {section} {key} must be true or false")
    return value


def _str_list(raw: dict, key: str, default: list[str], where: str, section: str) -> list[str]:
    value = raw.get(key, default)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{where}: {section} {key} must be a list of strings")
    return list(value)
