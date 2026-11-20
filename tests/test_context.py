from pathlib import Path

import pytest

from codegraph.cli import main
from codegraph.config import load_config
from codegraph.db import Database
from codegraph.indexer import build
from codegraph.server.tools import GraphTools
from codegraph.tokens import token_count


@pytest.fixture
def tools(config, summary):
    graph = GraphTools(config)
    yield graph
    graph.close()


def test_route_context_includes_both_sides(tools):
    result = tools.context("Trace GET /api/v1/tasks/{task_id}")
    assert "fetchTask" in result and "handler: get_task" in result
    assert "list_tasks" in result
    assert "handler: get_task" in tools.context("trace `fetchTask`")


def test_changed_symbol_prefers_call_sites(tools):
    result = tools.context("Что затронет изменение `TaskService.list_tasks`?")
    assert "Possible callers\n" in result
    assert "get_task" in result and "backend/app/api/tasks.py:12" in result
    assert (
        tools.get_callees("get_task")
        .splitlines()[0]
        .endswith("backend/app/services/task_service.py:7  [heuristic]")
    )


def test_paths_and_ambiguity(tools):
    assert "no declarations" not in tools.context("outline backend/app/models/task.py")
    assert "2 definitions" in tools.context("Task")
    assert "2 definitions" in tools.get_callers("Task")
    assert "list_tasks" in tools.get_callers("backend/app/models/task.py::Task")
    assert "list_tasks" not in tools.get_callers("frontend/src/types/task.ts::Task")


@pytest.mark.parametrize("budget", [1, 2, 12, 40, 75, 1200])
def test_context_honors_token_budget(tools, budget):
    assert 0 < token_count(tools.context("GET /api/v1/tasks/{task_id}", budget)) <= budget


def test_exact_fts_rank_and_cli_errors(config, summary, capsys):
    assert main(["context", "TaskService", "--config", str(config.source)]) == 0
    assert "TaskService" in capsys.readouterr().out
    assert main(["context", "Task", "--max-tokens", "0", "--config", str(config.source)]) == 2
    assert "positive" in capsys.readouterr().err


def test_source_changes_never_mix_old_lines_and_new_text(tools, project):
    f = project / "backend/app/models/task.py"
    src = f.read_text(encoding="utf-8")
    f.write_bytes(src.replace("\n", "\r\n").encode("utf-8"))
    assert "self.save()" in tools.get_definition("complete")
    f.write_text("# shifted\n" + src, encoding="utf-8")
    assert "Stale index" in tools.get_definition("complete")
    assert "Stale index" in tools.context("complete")
    assert "self.save()" not in tools.get_definition("complete")
    f.unlink()
    assert "Source unavailable" in tools.get_definition("complete")


def test_python_with_utf8_bom_is_indexed_and_can_be_read(project, config):
    path = project / "backend/app/models/task.py"
    path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8-sig")
    assert not any("models/task.py" in s for s in build(config).parse_errors)
    graph = GraphTools(config)
    try:
        assert "self.save()" in graph.get_definition("complete")
    finally:
        graph.close()


def test_receiver_evidence_and_reserved_path_characters(tmp_path: Path):
    root = tmp_path / "Код #1 %20"
    root.mkdir()
    (root / "demo.py").write_text(
        "class A:\n    def close(self): self.db.close()\n"
        "class B:\n    def close(self): pass\n"
        "def shutdown(b: 'B'):\n    b.close()\n"
        "def unknown(b):\n    b.close()\n"
        "def rebound(b: B):\n    b = other()\n    b.close()\n",
        encoding="utf-8",
    )
    config = load_config(cwd=root)
    build(config)
    build(config)
    with Database.open_readonly(config.db_path) as db:
        rows = db.conn.execute(
            "SELECT s.name,t.qualified_name,e.confidence FROM edges e "
            "JOIN nodes s ON s.id=e.src_id LEFT JOIN nodes t ON t.id=e.dst_id "
            "WHERE e.type='calls' AND e.dst_name='close'"
        ).fetchall()
    assert {r[0]: r[1] for r in rows} == {
        "close": None,
        "shutdown": "demo.py::B.close",
        "unknown": None,
        "rebound": None,
    }


def test_jedi_resolves_a_factory_return_across_files(tmp_path, monkeypatch):
    jedi = pytest.importorskip("jedi")
    monkeypatch.setattr(jedi.settings, "cache_directory", tmp_path / "jedi-cache")
    (tmp_path / ".codegraph.toml").write_text("[index]\npython_semantics=true\n", encoding="utf-8")
    (tmp_path / "factory.py").write_text(
        "class A:\n    def close(self): pass\n"
        "class B:\n    def close(self): pass\n"
        "def make() -> B:\n    return B()\n",
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text(
        "from factory import make\ndef shutdown():\n    make().close()\n", encoding="utf-8"
    )
    config = load_config(cwd=tmp_path)
    build(config)
    graph = GraphTools(config)
    try:
        assert "shutdown" in graph.get_callers("factory.py::B.close")
        assert "shutdown" not in graph.get_callers("factory.py::A.close")
    finally:
        graph.close()


def test_existing_v1_index_upgrades_with_an_incremental_build(config, summary):
    with Database.open(config.db_path) as old, old.transaction():
        old.conn.execute("ALTER TABLE edges DROP COLUMN receiver_type")
        old.conn.execute("PRAGMA user_version = 1")
    assert build(config).forced_full
    with Database.open_readonly(config.db_path) as graph:
        assert "receiver_type" in [
            r["name"] for r in graph.conn.execute("pragma table_info(edges)")
        ]


def test_search_prefers_exact_name_over_similar_test(project, config):
    (project / "backend/app/similar.py").write_text(
        "def source_fingerprint(): pass\n"
        "def test_source_fingerprint():\n    '''source_fingerprint'''\n",
        encoding="utf-8",
    )
    build(config)
    graph = GraphTools(config)
    try:
        assert (
            graph.search_symbol("source_fingerprint")
            .splitlines()[0]
            .startswith("source_fingerprint ")
        )
    finally:
        graph.close()
