"""Optional static member lookup through Jedi, restricted to indexed definitions."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from ..config import ConfigError
from ..db import Database
from .walker import hash_text, read_source


class PythonSemantics:
    def __init__(self, root: Path):
        try:
            import jedi
        except ImportError as exc:
            raise ConfigError(
                "python_semantics requires pip install 'codegraph[semantic]'"
            ) from exc

        self.jedi = jedi
        self.root = root
        self.project = jedi.Project(root, load_unsafe_extensions=False)
        self.path: Path | None = None
        self.script = None
        self.columns: dict[tuple[int, str], list[int]] = defaultdict(list)

    def target(self, db: Database, path: str, digest: str, line: int, name: str) -> int | None:
        source_path = self.root / path
        if self.path != source_path:
            self.path = source_path
            self.columns.clear()
            self.script = None
            try:
                text = read_source(source_path)
            except OSError:
                return None
            if hash_text(text) != digest:
                return None
            self.script = self.jedi.Script(text, path=source_path, project=self.project)
            for ref in self.script.get_names(all_scopes=True, definitions=False, references=True):
                self.columns[ref.line, ref.name].append(ref.column)
        positions = self.columns.get((line, name), [])
        if self.script is None or len(positions) != 1:
            return None
        targets = self.script.infer(line, positions[0])
        if len(targets) != 1:
            return None
        target = targets[0]
        if target.name != name or target.type != "function" or not target.module_path:
            return None
        try:
            rel = target.module_path.relative_to(self.root).as_posix()
        except ValueError:
            return None
        matches = db.conn.execute(
            "SELECT n.id FROM nodes n JOIN files f ON f.id=n.file_id WHERE f.path=? "
            "AND n.name=? AND n.type='method' AND ? BETWEEN n.line_start AND n.line_end",
            (rel, name, target.line),
        ).fetchall()
        return matches[0]["id"] if len(matches) == 1 else None
