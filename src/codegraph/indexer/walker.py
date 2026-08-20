"""File discovery, filtering and hashing.

Cross-platform rules enforced here, because everything downstream depends on
them:

* every path stored anywhere is relative to ``project.root`` and uses ``/``;
* files are read as UTF-8 explicitly with universal newlines, so a CRLF file
  and an LF file with the same content hash identically and report the same
  line numbers the editor shows;
* ignore patterns are matched against the POSIX form of the relative path.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

# extension -> value stored in files.language
LANGUAGE_BY_SUFFIX: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".php": "php",
}

# config language name -> the file.language values it enables
SUFFIXES_BY_LANGUAGE: dict[str, tuple[str, ...]] = {
    "python": (".py", ".pyi"),
    "typescript": (".ts", ".tsx"),
    "php": (".php",),
}


@dataclass(slots=True)
class DiscoveredFile:
    """One candidate file, already read and hashed."""

    path: str  # relative to root, POSIX separators
    abs_path: Path
    language: str
    content: str  # newline-normalised
    content_hash: str
    size_bytes: int


@dataclass(slots=True)
class SkippedFile:
    path: str
    reason: str


def rel_posix(path: Path, root: Path) -> str:
    """Path relative to ``root``, always with forward slashes."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        pass
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return Path(os.path.relpath(path, root)).as_posix()


def compile_ignore(patterns: Iterable[str]) -> list[re.Pattern[str]]:
    """Translate gitignore-ish globs into regexes matched against POSIX paths."""
    return [re.compile(_glob_to_regex(pattern)) for pattern in patterns]


def _glob_to_regex(pattern: str) -> str:
    pattern = pattern.replace("\\", "/")
    while pattern.startswith("./"):
        pattern = pattern[2:]
    if pattern.endswith("/"):
        pattern += "**"
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        char = pattern[i]
        if char == "*":
            if pattern.startswith("**/", i):
                out.append("(?:.*/)?")
                i += 3
                continue
            if pattern.startswith("**", i):
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
            i += 1
            continue
        if char == "?":
            out.append("[^/]")
            i += 1
            continue
        out.append(re.escape(char))
        i += 1
    return "^" + "".join(out) + "$"


def is_ignored(rel_path: str, patterns: list[re.Pattern[str]]) -> bool:
    return any(pattern.match(rel_path) for pattern in patterns)


def normalize_text(raw: str) -> str:
    """Collapse CRLF/CR to LF so hashes and line numbers are platform-stable."""
    return raw.replace("\r\n", "\n").replace("\r", "\n")


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_source(path: Path) -> str:
    """Read a source file as UTF-8 with normalised newlines.

    ``newline=""`` plus an explicit normalisation keeps the behaviour identical
    on Windows and Linux; undecodable bytes are replaced rather than raising,
    so one broken file cannot abort a build.
    """
    with open(path, encoding="utf-8", errors="replace", newline="") as handle:
        return normalize_text(handle.read())


def _include_roots(root: Path, include: Iterable[str]) -> list[Path]:
    roots: list[Path] = []
    for entry in include:
        candidate = (root / entry).resolve() if not Path(entry).is_absolute() else Path(entry)
        if candidate.exists():
            roots.append(candidate)
    return roots


def _wanted_suffixes(languages: Iterable[str]) -> set[str]:
    suffixes: set[str] = set()
    for language in languages:
        suffixes.update(SUFFIXES_BY_LANGUAGE.get(language, ()))
    return suffixes


def walk(
    root: Path,
    include: Iterable[str],
    languages: Iterable[str],
    exclude: Iterable[str],
    max_file_size_kb: int = 512,
) -> tuple[list[DiscoveredFile], list[SkippedFile]]:
    """Discover, read and hash every indexable file.

    Returns the files plus the ones deliberately skipped (too large, unreadable).
    """
    root = root.resolve()
    patterns = compile_ignore(exclude)
    suffixes = _wanted_suffixes(languages)
    max_bytes = max_file_size_kb * 1024

    found: dict[str, DiscoveredFile] = {}
    skipped: list[SkippedFile] = []

    for start in _include_roots(root, include):
        for abs_path in _iter_candidates(start, root, patterns, suffixes):
            rel = rel_posix(abs_path, root)
            if rel in found:
                continue
            try:
                stat = abs_path.stat()
            except OSError as exc:
                skipped.append(SkippedFile(rel, f"cannot stat: {exc}"))
                continue
            if stat.st_size > max_bytes:
                skipped.append(
                    SkippedFile(rel, f"larger than max_file_size_kb ({stat.st_size // 1024} KB)")
                )
                continue
            try:
                content = read_source(abs_path)
            except OSError as exc:
                skipped.append(SkippedFile(rel, f"cannot read: {exc}"))
                continue
            found[rel] = DiscoveredFile(
                path=rel,
                abs_path=abs_path,
                language=LANGUAGE_BY_SUFFIX[abs_path.suffix.lower()],
                content=content,
                content_hash=hash_text(content),
                size_bytes=len(content.encode("utf-8")),
            )

    return sorted(found.values(), key=lambda f: f.path), skipped


def _iter_candidates(
    start: Path,
    root: Path,
    patterns: list[re.Pattern[str]],
    suffixes: set[str],
) -> Iterator[Path]:
    if start.is_file():
        if start.suffix.lower() in suffixes and not is_ignored(rel_posix(start, root), patterns):
            yield start
        return

    for dirpath, dirnames, filenames in os.walk(start):
        current = Path(dirpath)
        # Prune ignored directories in place so os.walk never descends into them.
        kept: list[str] = []
        for name in dirnames:
            rel_dir = rel_posix(current / name, root)
            if is_ignored(rel_dir, patterns) or is_ignored(rel_dir + "/", patterns):
                continue
            kept.append(name)
        dirnames[:] = kept

        for name in filenames:
            file_path = current / name
            if file_path.suffix.lower() not in suffixes:
                continue
            if is_ignored(rel_posix(file_path, root), patterns):
                continue
            yield file_path
