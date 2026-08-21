"""CLI behaviour, including the failure paths that must not show a traceback."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from codegraph.cli import main
from codegraph.config import Config


def test_build_reports_a_summary(project: Path, capsys: pytest.CaptureFixture[str]):
    exit_code = main(["build", "--config", str(project), "--full"])
    output = capsys.readouterr().out
    assert exit_code == 0
    assert "files:" in output and "graph:" in output
    assert "bridge: 2 endpoints" in output
    assert (project / ".codegraph" / "graph.db").exists()


def test_build_is_incremental_on_a_second_run(project: Path, capsys: pytest.CaptureFixture[str]):
    main(["build", "--config", str(project), "--full"])
    capsys.readouterr()
    main(["build", "--config", str(project)])
    assert "0 parsed" in capsys.readouterr().out


def test_verbose_build_logs_each_file(project: Path, capsys: pytest.CaptureFixture[str]):
    main(["build", "--config", str(project), "--full", "--verbose"])
    assert "parsed backend/app/models/task.py" in capsys.readouterr().out


def test_stats_breaks_the_graph_down(config: Config, summary, capsys: pytest.CaptureFixture[str]):
    exit_code = main(["stats", "--config", str(config.root)])
    output = capsys.readouterr().out
    assert exit_code == 0
    assert "nodes by type:" in output
    assert "edges by type:" in output
    assert "endpoint" in output
    assert "most used third-party symbols:" in output
    assert "most common unknown targets" in output


def test_query_searches_and_shows_definitions(
    config: Config, summary, capsys: pytest.CaptureFixture[str]
):
    main(["query", "TaskService", "--config", str(config.root)])
    assert "backend/app/services/task_service.py" in capsys.readouterr().out

    main(["query", "complete", "--definition", "--config", str(config.root)])
    assert "Mark the task done." in capsys.readouterr().out


def test_a_bad_config_exits_cleanly(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    (tmp_path / ".codegraph.toml").write_text("[index]\nlanguages = ['cobol']\n", encoding="utf-8")
    exit_code = main(["build", "--config", str(tmp_path)])
    assert exit_code == 2
    assert capsys.readouterr().err.startswith("error: ")


def test_serving_without_a_graph_exits_cleanly(project: Path, capsys: pytest.CaptureFixture[str]):
    exit_code = main(["serve", "--config", str(project)])
    assert exit_code == 2
    assert "Run 'codegraph build' first" in capsys.readouterr().err


def test_stats_without_a_graph_exits_cleanly(project: Path, capsys: pytest.CaptureFixture[str]):
    exit_code = main(["stats", "--config", str(project)])
    assert exit_code == 2
    assert "error: " in capsys.readouterr().err


def test_a_stale_schema_asks_for_a_full_rebuild(
    config: Config, summary, capsys: pytest.CaptureFixture[str]
):
    connection = sqlite3.connect(config.db_path)
    connection.execute("PRAGMA user_version = 99")
    connection.commit()
    connection.close()

    exit_code = main(["stats", "--config", str(config.root)])
    assert exit_code == 2
    assert "codegraph build --full" in capsys.readouterr().err


def test_a_locked_database_is_reported_not_traced(
    project: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
):
    """The message has to name the likely culprit; a traceback names nothing."""
    import sqlite3

    import codegraph.indexer

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(codegraph.indexer, "build", locked)
    exit_code = main(["build", "--config", str(project)])
    error = capsys.readouterr().err
    assert exit_code == 2
    assert "database is locked" in error
    assert "MCP server" in error
    assert "Traceback" not in error
