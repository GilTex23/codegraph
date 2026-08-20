"""Build pipeline: walk -> parse -> resolve -> bridge.

``build`` is incremental by default.  A file whose sha256 matches what is
already in the database is not re-parsed; deleted files are purged.  Resolution
always runs over the whole project, because one edited file can change how
names resolve elsewhere -- and it is cheap compared to parsing.
"""

from __future__ import annotations

import functools
import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .. import __version__
from ..config import Config
from ..db import Database
from ..models import ParseResult
from .bridge import BridgeStats, run_bridge
from .python_parser import parse_python
from .resolver import ResolveStats, resolve
from .ts_parser import parse_typescript
from .walker import walk

__all__ = ["BuildSummary", "build"]


@dataclass(slots=True)
class BuildSummary:
    forced_full: bool = False
    parsed: int = 0
    unchanged: int = 0
    removed: int = 0
    files: int = 0
    nodes: int = 0
    edges: int = 0
    unresolved_edges: int = 0
    heuristic_edges: int = 0
    external_edges: int = 0
    dropped_builtin_edges: int = 0
    duration_seconds: float = 0.0
    problems: list[str] = field(default_factory=list)
    parse_errors: list[str] = field(default_factory=list)
    bridge: BridgeStats = field(default_factory=BridgeStats)

    def render(self) -> str:
        lines = [
            f"files:  {self.files} indexed  "
            f"({self.parsed} parsed, {self.unchanged} unchanged, {self.removed} removed)",
            f"graph:  {self.nodes} nodes, {self.edges} edges  "
            f"({self.heuristic_edges} heuristic, {self.external_edges} third-party, "
            f"{self.unresolved_edges - self.external_edges} unknown)"
            + (
                f"  {self.dropped_builtin_edges} builtin calls dropped"
                if self.dropped_builtin_edges
                else ""
            ),
        ]
        if self.bridge.endpoints or self.bridge.api_calls:
            lines.append(
                f"bridge: {self.bridge.endpoints} endpoints "
                f"({self.bridge.handled} with handlers), "
                f"{self.bridge.api_calls_matched}/{self.bridge.api_calls} frontend calls matched"
            )
        for label, items in (("skipped", self.problems), ("parsed with errors", self.parse_errors)):
            if not items:
                continue
            lines.append(f"{label}: {len(items)} file(s)")
            lines.extend(f"  - {item}" for item in items[:10])
            if len(items) > 10:
                lines.append(f"  ... and {len(items) - 10} more")
        if self.forced_full:
            lines.append("(rebuilt from scratch: codegraph itself changed)")
        lines.append(f"done in {self.duration_seconds:.2f}s")
        return "\n".join(lines)


def fingerprint_sources(package: Path) -> str:
    """Hash every ``.py`` under ``package``, newline-normalised."""
    digest = hashlib.sha256(__version__.encode())
    for path in sorted(package.rglob("*.py")):
        digest.update(path.relative_to(package).as_posix().encode())
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()[:16]


@functools.cache
def source_fingerprint() -> str:
    """Identity of the codegraph *code*, not of its version number.

    An editable install runs straight from a working tree, where the version
    string sits still while the resolver changes underneath it -- so keying
    staleness on ``__version__`` would quietly serve graphs built by older
    logic. Hashing the package's own sources means any edit to the tool
    invalidates the graphs it produced, with no discipline required from
    whoever edits it.
    """
    try:
        return fingerprint_sources(Path(__file__).resolve().parent.parent)
    except OSError:  # frozen or unreadable install: the version is all we have
        return __version__


def _built_by_another_build(db_path) -> bool:
    """True when an existing graph was written by different codegraph code."""
    if not Path(db_path).exists():
        return False
    with Database.open(db_path) as db:
        return db.get_meta("codegraph_build") != source_fingerprint()


def parse_source(rel_path: str, source: str, language: str) -> ParseResult:
    """Dispatch to the parser for a file's language."""
    if language == "python":
        return parse_python(rel_path, source)
    return parse_typescript(rel_path, source, language)


def build(
    config: Config,
    *,
    full: bool = False,
    verbose: bool = False,
    log: Callable[[str], None] = print,
) -> BuildSummary:
    """Index the project described by ``config`` into its graph database."""
    started = time.perf_counter()
    summary = BuildSummary()

    # A codegraph upgrade can change how names resolve or what the parsers
    # extract, and unchanged files are otherwise never revisited.  Rebuild once,
    # automatically, rather than leaving a subtly stale graph behind.
    if not full and _built_by_another_build(config.db_path):
        full = True
        summary.forced_full = True

    discovered, skipped = walk(
        root=config.root,
        include=config.index.include,
        languages=config.index.languages,
        exclude=config.index.exclude,
        max_file_size_kb=config.index.max_file_size_kb,
    )
    summary.problems.extend(f"{item.path}: {item.reason}" for item in skipped)

    with Database.open(config.db_path, reset=full) as db:
        existing = db.all_files()

        with db.transaction():
            for file in discovered:
                previous = existing.get(file.path)
                if previous is not None and previous.content_hash == file.content_hash:
                    summary.unchanged += 1
                    continue
                file_id = db.upsert_file(
                    file.path, file.language, file.content_hash, file.size_bytes
                )
                result = parse_source(file.path, file.content, file.language)
                db.write_parse_result(file_id, result)
                summary.parsed += 1
                summary.parse_errors.extend(result.errors)
                if verbose:
                    log(f"  parsed {file.path} ({len(result.nodes)} nodes)")

            discovered_paths = {file.path for file in discovered}
            stale = [path for path in existing if path not in discovered_paths]
            summary.removed = db.delete_files(stale)
            if verbose:
                for path in stale:
                    log(f"  removed {path}")

        resolve_stats: ResolveStats = resolve(db, config.root)
        summary.heuristic_edges = resolve_stats.heuristic
        summary.dropped_builtin_edges = resolve_stats.dropped_builtins
        summary.external_edges = resolve_stats.external

        summary.bridge = run_bridge(db, config)

        db.set_meta("codegraph_version", __version__)
        db.set_meta("codegraph_build", source_fingerprint())
        counts = db.counts()
        summary.files = counts["files"]
        summary.nodes = counts["nodes"]
        summary.edges = counts["edges"]
        summary.unresolved_edges = counts["unresolved_edges"]
        db.optimize()

    summary.duration_seconds = time.perf_counter() - started
    return summary
