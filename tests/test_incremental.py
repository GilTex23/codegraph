"""Incremental rebuilds, path normalisation and hashing stability."""

from __future__ import annotations

from pathlib import Path

from codegraph.config import Config, load_config
from codegraph.db import Database
from codegraph.indexer import build, fingerprint_sources, source_fingerprint
from codegraph.indexer.walker import hash_text, read_source


def node_names(graph: Database, rel_path: str) -> set[str]:
    rows = graph.conn.execute(
        "SELECT n.name FROM nodes n JOIN files f ON f.id = n.file_id WHERE f.path = ?",
        (rel_path,),
    ).fetchall()
    return {row["name"] for row in rows}


def test_unchanged_files_are_skipped_on_the_second_build(config: Config, summary):
    again = build(config)
    assert again.parsed == 0
    assert again.unchanged == summary.parsed
    assert again.nodes == summary.nodes
    assert again.edges == summary.edges


def test_editing_a_file_updates_its_nodes(config: Config, summary):
    target = config.root / "backend" / "app" / "models" / "task.py"
    target.write_text(
        "from .base import Base\n\n\nclass Task(Base):\n"
        '    """A unit of work."""\n\n'
        "    def reopen(self) -> None:\n        self.save()\n",
        encoding="utf-8",
        newline="\n",
    )
    again = build(config)
    assert again.parsed == 1
    assert again.unchanged == summary.parsed - 1

    with Database.open_readonly(config.db_path) as graph:
        names = node_names(graph, "backend/app/models/task.py")
    assert "reopen" in names
    assert "complete" not in names  # the old declaration is gone
    assert "STATUS_DONE" not in names


def test_deleting_a_file_purges_its_nodes_and_edges(config: Config, summary):
    (config.root / "backend" / "app" / "models" / "task.py").unlink()
    again = build(config)
    assert again.removed == 1

    with Database.open_readonly(config.db_path) as graph:
        assert node_names(graph, "backend/app/models/task.py") == set()
        orphaned = graph.conn.execute(
            "SELECT count(*) FROM edges e LEFT JOIN nodes s ON s.id = e.src_id WHERE s.id IS NULL"
        ).fetchone()[0]
    assert orphaned == 0


def test_edges_into_an_edited_file_survive_the_rebuild(config: Config, summary):
    """Regression guard: re-indexing a file must not drop inbound edges.

    ``task_service.py`` calls ``Task``; editing ``task.py`` deletes and recreates
    the Task node, and a cascading delete on ``edges.dst_id`` would silently
    take the caller's edge with it.
    """
    target = config.root / "backend" / "app" / "models" / "task.py"
    target.write_text(
        target.read_text(encoding="utf-8") + "\n\nEXTRA = 1\n", encoding="utf-8", newline="\n"
    )
    build(config)

    with Database.open_readonly(config.db_path) as graph:
        row = graph.conn.execute(
            "SELECT e.resolved, tf.path FROM edges e "
            "JOIN nodes s ON s.id = e.src_id "
            "LEFT JOIN nodes t ON t.id = e.dst_id LEFT JOIN files tf ON tf.id = t.file_id "
            "WHERE e.type = 'calls' AND s.name = 'list_tasks' AND e.dst_name = 'Task'"
        ).fetchone()
    assert row is not None
    assert row["resolved"] == 1
    assert row["path"] == "backend/app/models/task.py"


def test_full_rebuild_starts_from_an_empty_database(config: Config, summary):
    (config.root / "backend" / "app" / "models" / "task.py").unlink()
    again = build(config, full=True)
    assert again.unchanged == 0
    assert again.removed == 0
    assert again.files == summary.files - 1


def test_no_backslashes_reach_the_database(graph: Database):
    paths = [row["path"] for row in graph.conn.execute("SELECT path FROM files")]
    qualified = [
        row["qualified_name"] for row in graph.conn.execute("SELECT qualified_name FROM nodes")
    ]
    assert paths, "expected an indexed project"
    assert all("\\" not in path for path in paths)
    assert all("\\" not in name for name in qualified)


def test_paths_are_relative_to_the_project_root(graph: Database):
    paths = [row["path"] for row in graph.conn.execute("SELECT path FROM files")]
    assert all(not Path(path).is_absolute() for path in paths)
    assert "backend/app/models/task.py" in paths


def test_crlf_and_lf_hash_identically(tmp_path: Path):
    text = "def f():\n    return 1\n"
    lf = tmp_path / "lf.py"
    crlf = tmp_path / "crlf.py"
    lf.write_bytes(text.encode("utf-8"))
    crlf.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
    assert hash_text(read_source(lf)) == hash_text(read_source(crlf))


def test_line_numbers_match_the_editor_for_crlf_files(tmp_path: Path):
    root = tmp_path / "crlf_project"
    root.mkdir()
    (root / "mod.py").write_bytes(
        b"# header\r\n# header\r\n\r\n\r\ndef target():\r\n    return 1\r\n"
    )
    config = load_config(cwd=root)
    build(config, full=True)
    with Database.open_readonly(config.db_path) as graph:
        row = graph.conn.execute("SELECT line_start FROM nodes WHERE name = 'target'").fetchone()
    assert row["line_start"] == 5


def test_utf8_is_read_regardless_of_the_system_locale(tmp_path: Path):
    root = tmp_path / "cyrillic"
    root.mkdir()
    (root / "mod.py").write_text(
        'def считать():\n    """Считает всё."""\n    return 1\n',
        encoding="utf-8",
        newline="\n",
    )
    config = load_config(cwd=root)
    summary = build(config, full=True)
    assert summary.problems == []
    with Database.open_readonly(config.db_path) as graph:
        row = graph.conn.execute(
            "SELECT name, docstring FROM nodes WHERE type = 'function'"
        ).fetchone()
    assert row["name"] == "считать"
    assert row["docstring"] == "Считает всё."


def test_a_broken_file_does_not_fail_the_build(summary):
    assert summary.files > 0
    assert len(summary.parse_errors) == 2  # one Python, one TypeScript
    assert all("broken" in error for error in summary.parse_errors)
    assert summary.problems == []  # nothing was actually skipped


def test_excluded_directories_are_never_walked(config: Config, summary):
    junk = config.root / "frontend" / "node_modules" / "pkg"
    junk.mkdir(parents=True)
    (junk / "index.ts").write_text("export const x = 1;\n", encoding="utf-8", newline="\n")
    again = build(config)
    with Database.open_readonly(config.db_path) as graph:
        paths = [row["path"] for row in graph.conn.execute("SELECT path FROM files")]
    assert not any("node_modules" in path for path in paths)
    assert again.parsed == 0


def test_a_codegraph_change_forces_one_full_rebuild(config: Config, summary):
    """Unchanged files are never revisited, so editing the tool must reset the graph."""
    with Database.open(config.db_path) as db:
        db.set_meta("codegraph_build", "written-by-older-code")
        db.conn.commit()

    again = build(config)
    assert again.forced_full is True
    assert again.unchanged == 0
    assert again.parsed == summary.parsed
    assert "rebuilt from scratch" in again.render()

    once_more = build(config)
    assert once_more.forced_full is False
    assert once_more.parsed == 0


def test_the_build_records_the_fingerprint_of_the_code_that_made_it(config: Config, summary):
    with Database.open(config.db_path) as db:
        assert db.get_meta("codegraph_build") == source_fingerprint()
        assert db.get_meta("codegraph_version")  # kept for humans reading the file


def test_the_fingerprint_tracks_source_edits(tmp_path: Path):
    """Version numbers stand still during editable development; the code does not."""
    package = tmp_path / "pkg"
    (package / "sub").mkdir(parents=True)
    (package / "a.py").write_text("x = 1\n", encoding="utf-8")
    (package / "sub" / "b.py").write_text("y = 2\n", encoding="utf-8")

    original = fingerprint_sources(package)
    assert fingerprint_sources(package) == original  # deterministic

    (package / "sub" / "b.py").write_text("y = 3\n", encoding="utf-8")
    assert fingerprint_sources(package) != original


def test_the_fingerprint_ignores_line_endings(tmp_path: Path):
    """A CRLF checkout of identical code must not look like a different tool."""
    lf, crlf = tmp_path / "lf", tmp_path / "crlf"
    lf.mkdir()
    crlf.mkdir()
    (lf / "a.py").write_bytes(b"def f():\n    return 1\n")
    (crlf / "a.py").write_bytes(b"def f():\r\n    return 1\r\n")
    assert fingerprint_sources(lf) == fingerprint_sources(crlf)


def test_the_fingerprint_covers_the_whole_package(tmp_path: Path):
    """Renaming a file changes the tool even when the bytes are the same."""
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "a.py").write_text("x = 1\n", encoding="utf-8")
    before = fingerprint_sources(package)
    (package / "a.py").rename(package / "renamed.py")
    assert fingerprint_sources(package) != before


def test_a_full_rebuild_works_while_a_reader_holds_the_database(config: Config, summary):
    """An MCP server keeps the graph open for the whole agent session.

    Windows refuses to unlink an open file, so a rebuild that deleted the
    database failed precisely when codegraph was being used. Dropping the
    objects instead works underneath a reader.
    """
    reader = Database.open_readonly(config.db_path)
    try:
        before = reader.counts()["nodes"]
        assert before > 0

        again = build(config, full=True)  # must not raise PermissionError
        assert again.nodes == summary.nodes

        # The same connection now sees the rebuilt graph, not a stale snapshot.
        assert reader.counts()["nodes"] == again.nodes
    finally:
        reader.close()


def test_a_reader_does_not_block_an_incremental_build(config: Config, summary):
    reader = Database.open_readonly(config.db_path)
    try:
        target = config.root / "backend" / "app" / "models" / "task.py"
        target.write_text(
            target.read_text(encoding="utf-8") + "\n\nEXTRA = 1\n",
            encoding="utf-8",
            newline="\n",
        )
        again = build(config)
        assert again.parsed == 1
        assert reader.counts()["nodes"] == again.nodes
    finally:
        reader.close()


def test_a_full_rebuild_recovers_from_a_stale_schema(config: Config, summary):
    """--full is what the schema-mismatch error tells you to run, so it must work."""
    with Database.open(config.db_path) as db:
        db.conn.execute("PRAGMA user_version = 99")
        db.conn.commit()

    again = build(config, full=True)
    assert again.nodes == summary.nodes
