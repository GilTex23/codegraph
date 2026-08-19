"""Edge resolution: all four branches, plus re-export chains."""

from __future__ import annotations

from pathlib import Path

from codegraph.config import load_config
from codegraph.db import Database
from codegraph.indexer import build
from helpers import edge_rows, find_edge


def calls(graph: Database) -> list[dict]:
    return edge_rows(graph, "calls")


def test_branch_1_same_file_declaration_is_exact(graph: Database):
    """`Base.save` calls `commit`, declared in the same file."""
    edge = find_edge(calls(graph), "save", "commit")
    assert edge["resolved"] == 1
    assert edge["confidence"] == "exact"
    assert edge["dst_path"] == "backend/app/models/base.py"


def test_branch_2_explicit_import_is_exact(graph: Database):
    """`TaskService.list_tasks` calls `Task()`, imported from app.models."""
    edge = find_edge(calls(graph), "list_tasks", "Task")
    assert edge["resolved"] == 1
    assert edge["confidence"] == "exact"


def test_branch_3_globally_unique_name_is_heuristic(graph: Database):
    """`get_task` calls `service.list_tasks()`; only one such name exists."""
    edge = find_edge(calls(graph), "get_task", "list_tasks")
    assert edge["resolved"] == 1
    assert edge["confidence"] == "heuristic"


def test_branch_4_unknown_name_stays_unresolved_but_keeps_dst_name(graph: Database):
    edge = find_edge(calls(graph), "tasks", "APIRouter")
    assert edge["resolved"] == 0
    assert edge["dst_path"] is None
    assert edge["dst_name"] == "APIRouter"


def test_every_unresolved_edge_still_names_its_target(graph: Database):
    rows = graph.conn.execute(
        "SELECT count(*) FROM edges WHERE resolved = 0 AND (dst_name IS NULL OR dst_name = '')"
    ).fetchone()
    assert rows[0] == 0


def test_python_reexport_chain_reaches_the_real_declaration(graph: Database):
    """`from app.models import Task` must not stop at models/__init__.py."""
    edge = find_edge(calls(graph), "list_tasks", "Task")
    assert edge["dst_path"] == "backend/app/models/task.py"
    assert edge["dst_type"] == "class"


def test_typescript_barrel_reexport_chain(graph: Database):
    """`import { fetchTask } from '../api'` must reach api/tasks.ts."""
    edge = find_edge(calls(graph), "TaskList", "fetchTask")
    assert edge["resolved"] == 1
    assert edge["confidence"] == "exact"
    assert edge["dst_path"] == "frontend/src/api/tasks.ts"


def test_inheritance_across_files(graph: Database):
    edge = find_edge(edge_rows(graph, "inherits"), "Task", "Base")
    assert edge["resolved"] == 1
    assert edge["dst_path"] == "backend/app/models/base.py"


def test_relative_typescript_import_resolves_to_a_file(graph: Database):
    edge = find_edge(edge_rows(graph, "imports"), "tasks", "./client")
    assert edge["resolved"] == 1
    assert edge["dst_path"] == "frontend/src/api/client.ts"


def test_bare_package_imports_stay_unresolved(graph: Database):
    edge = find_edge(edge_rows(graph, "imports"), "TaskList", "react")
    assert edge["resolved"] == 0
    assert edge["dst_name"] == "react"


def test_relative_python_import_resolves(graph: Database):
    edge = find_edge(edge_rows(graph, "imports"), "task", ".base")
    assert edge["resolved"] == 1
    assert edge["dst_path"] == "backend/app/models/base.py"


def test_reexport_cycle_does_not_hang(tmp_path: Path):
    """Two barrels re-exporting each other must terminate."""
    root = tmp_path / "cyclic"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("from .a import Thing\n", encoding="utf-8")
    (root / "pkg" / "a.py").write_text("from .b import Thing\n", encoding="utf-8")
    (root / "pkg" / "b.py").write_text("from .a import Thing\n", encoding="utf-8")
    (root / "use.py").write_text(
        "from pkg import Thing\n\n\ndef go():\n    Thing()\n", encoding="utf-8"
    )

    config = load_config(cwd=root)
    build(config, full=True)
    with Database.open_readonly(config.db_path) as graph:
        edge = find_edge(calls(graph), "go", "Thing")
    assert edge["resolved"] == 0  # nothing actually declares it, and we did not loop


def test_tsconfig_path_aliases_are_honoured(tmp_path: Path):
    root = tmp_path / "aliased"
    (root / "src" / "lib").mkdir(parents=True)
    (root / "tsconfig.json").write_text(
        '{\n  // comment tolerated\n  "compilerOptions": {'
        '"baseUrl": ".", "paths": {"@/*": ["src/*"]}}\n}\n',
        encoding="utf-8",
    )
    (root / "src" / "lib" / "math.ts").write_text(
        "export function addUp(a: number) {\n  return a;\n}\n", encoding="utf-8"
    )
    (root / "src" / "app.ts").write_text(
        "import { addUp } from '@/lib/math';\n\nexport function run() {\n  return addUp(1);\n}\n",
        encoding="utf-8",
    )

    config = load_config(cwd=root)
    build(config, full=True)
    with Database.open_readonly(config.db_path) as graph:
        edge = find_edge(calls(graph), "run", "addUp")
    assert edge["resolved"] == 1
    assert edge["confidence"] == "exact"
    assert edge["dst_path"] == "src/lib/math.ts"


def test_self_call_prefers_a_sibling_method(tmp_path: Path):
    root = tmp_path / "siblings"
    root.mkdir()
    (root / "a.py").write_text(
        "class A:\n"
        "    def run(self):\n"
        "        self.step()\n\n"
        "    def step(self):\n"
        "        pass\n\n\n"
        "class B:\n"
        "    def step(self):\n"
        "        pass\n",
        encoding="utf-8",
    )
    config = load_config(cwd=root)
    build(config, full=True)
    with Database.open_readonly(config.db_path) as graph:
        row = graph.conn.execute(
            "SELECT t.qualified_name FROM edges e JOIN nodes t ON t.id = e.dst_id "
            "WHERE e.type = 'calls' AND e.dst_name = 'step'"
        ).fetchone()
    assert row["qualified_name"] == "a.py::A.step"


def test_builtin_calls_are_dropped_but_shadowed_names_survive(tmp_path: Path):
    """`len(x)` is noise; a project that defines its own `len` keeps the edge."""
    root = tmp_path / "builtins_project"
    root.mkdir()
    (root / "a.py").write_text(
        "def plain():\n    return len([1]) + int('2')\n",
        encoding="utf-8",
    )
    (root / "b.py").write_text(
        "def sorted(items):\n    return items\n\n\ndef uses():\n    return sorted([3, 1])\n",
        encoding="utf-8",
    )
    config = load_config(cwd=root)
    summary = build(config, full=True)
    assert summary.dropped_builtin_edges == 2  # len and int

    with Database.open_readonly(config.db_path) as graph:
        names = {row["dst_name"] for row in calls(graph)}
    assert "len" not in names
    assert "int" not in names
    assert "sorted" in names  # shadowed by a project declaration


def test_third_party_symbols_are_marked_external_not_unknown(graph: Database):
    """`useState` comes from react; that is not a gap in the graph."""
    react = [row for row in edge_rows(graph, "imports") if row["dst_name"] == "react"]
    assert react and all(row["confidence"] == "external" for row in react)


def test_a_call_to_an_imported_library_symbol_is_external(tmp_path: Path):
    root = tmp_path / "external_project"
    root.mkdir()
    (root / "a.py").write_text(
        "from fastapi import Depends\n\n\ndef handler():\n    return Depends(None)\n",
        encoding="utf-8",
    )
    config = load_config(cwd=root)
    summary = build(config, full=True)
    assert summary.external_edges >= 1

    with Database.open_readonly(config.db_path) as graph:
        edge = find_edge(calls(graph), "handler", "Depends")
    assert edge["resolved"] == 0
    assert edge["confidence"] == "external"


def test_an_unresolvable_project_name_stays_unknown(tmp_path: Path):
    root = tmp_path / "unknown_project"
    root.mkdir()
    (root / "a.py").write_text("def handler():\n    return whoIsThis()\n", encoding="utf-8")
    config = load_config(cwd=root)
    build(config, full=True)
    with Database.open_readonly(config.db_path) as graph:
        edge = find_edge(calls(graph), "handler", "whoIsThis")
    assert edge["resolved"] == 0
    assert edge["confidence"] != "external"


def test_the_unique_name_heuristic_never_crosses_languages(tmp_path: Path):
    """A Python `db.add(...)` must not resolve to a TypeScript `add`.

    A confidently wrong edge is worse than a missing one: the agent has no way
    to tell it is wrong.
    """
    root = tmp_path / "mixed"
    (root / "src").mkdir(parents=True)
    (root / "back.py").write_text("def handler(db):\n    return db.add(1)\n", encoding="utf-8")
    (root / "src" / "helpers.ts").write_text(
        "export function add(a: number) {\n  return a;\n}\n", encoding="utf-8"
    )
    config = load_config(cwd=root)
    build(config, full=True)
    with Database.open_readonly(config.db_path) as graph:
        edge = find_edge(calls(graph), "handler", "add")
    assert edge["resolved"] == 0
    assert edge["dst_path"] is None
