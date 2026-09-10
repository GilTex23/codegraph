"""``codegraph instructions``: the block, and how it lands in an agent file."""

from __future__ import annotations

from pathlib import Path

import pytest

from codegraph.cli import main
from codegraph.config import Config, load_config
from codegraph.indexer import build
from codegraph.instructions import (
    MARKER_END,
    MARKER_START,
    claude_reads,
    default_target,
    insert,
    render,
    write_block,
)


def bulk(project: Path, name: str = "huge.py", functions: int = 800) -> Path:
    """A file big enough that reading it is a real decision."""
    path = project / "backend" / "app" / name
    path.write_text(
        "\n".join(f"def generated_{i}():\n    return {i}\n" for i in range(functions)),
        encoding="utf-8",
    )
    return path


# ------------------------------------------------------------------- the block


def test_the_block_names_every_tool_worth_reaching_for(config: Config):
    block = render(config)
    assert block.startswith(MARKER_START)
    assert block.rstrip().endswith(MARKER_END)
    for call in ("get_project_overview()", "get_file_outline(", "get_callers(", "get_neighbors("):
        assert call in block


def test_the_block_stands_up_before_the_first_build(config: Config):
    """No graph yet is not an error -- just no numbers that need one."""
    assert not config.db_path.exists()
    block = render(config)
    assert "get_project_overview()" in block
    assert "measured on this graph" not in block


def test_the_cost_table_measures_the_heaviest_files(config: Config, project: Path):
    bulk(project)
    build(config, full=True)

    block = render(config)
    assert "measured on this graph" in block
    row = next(line for line in block.splitlines() if "huge.py" in line)
    read, outline = (cell.strip() for cell in row.split("|")[2:4])
    # The ratio is the argument the block is making; assert it holds.
    assert read.endswith("k tokens")
    assert float(read.removeprefix("~").removesuffix("k tokens")) > 5
    assert outline.startswith("~")


def test_routes_are_offered_only_where_there_are_routes(config: Config, tmp_path: Path):
    assert "trace_endpoint" in render(config)  # the sample project has a bridge
    plain = load_config(cwd=tmp_path)
    assert "trace_endpoint" not in render(plain)


# ------------------------------------------------------------------ insertion


def test_a_first_insert_appends_and_keeps_what_was_there():
    updated = insert("# Project\n\nSome prose.\n", "BLOCK\n")
    assert updated == "# Project\n\nSome prose.\n\nBLOCK\n"


def test_a_second_insert_replaces_the_block_in_place():
    original = insert("# Project\n", f"{MARKER_START}\nold\n{MARKER_END}\n")
    updated = insert(original, f"{MARKER_START}\nnew\n{MARKER_END}\n")
    assert updated.count(MARKER_START) == 1
    assert "old" not in updated and "new" in updated
    assert updated.startswith("# Project\n")


def test_prose_after_the_block_survives_a_refresh():
    original = f"# Project\n\n{MARKER_START}\nold\n{MARKER_END}\n\n## Conventions\n\nMine.\n"
    updated = insert(original, f"{MARKER_START}\nnew\n{MARKER_END}\n")
    assert updated.endswith("## Conventions\n\nMine.\n")
    assert "new" in updated and "old" not in updated


def test_write_block_reports_whether_it_replaced_one(tmp_path: Path, config: Config):
    agents = tmp_path / "AGENTS.md"
    agents.write_text("# Rules\n", encoding="utf-8")
    block = render(config)
    assert write_block(agents, block) is False
    assert write_block(agents, block) is True
    assert agents.read_text(encoding="utf-8").count(MARKER_START) == 1


def test_the_agent_file_already_present_is_the_default_target(tmp_path: Path):
    assert default_target(tmp_path) is None
    (tmp_path / "CLAUDE.md").write_text("x\n", encoding="utf-8")
    assert default_target(tmp_path).name == "CLAUDE.md"
    (tmp_path / "AGENTS.md").write_text("x\n", encoding="utf-8")
    assert default_target(tmp_path).name == "AGENTS.md"


def test_claude_only_reads_agents_md_through_an_import(tmp_path: Path):
    agents = tmp_path / "AGENTS.md"
    agents.write_text("x\n", encoding="utf-8")
    assert claude_reads(tmp_path, agents) is False

    claude = tmp_path / "CLAUDE.md"
    claude.write_text("# Notes\n", encoding="utf-8")
    assert claude_reads(tmp_path, agents) is False
    assert claude_reads(tmp_path, claude) is True

    claude.write_text("@AGENTS.md\n", encoding="utf-8")
    assert claude_reads(tmp_path, agents) is True


# ------------------------------------------------------------------- the CLI


def test_cli_prints_the_block(project: Path, capsys: pytest.CaptureFixture[str]):
    assert main(["instructions", "--config", str(project)]) == 0
    output = capsys.readouterr().out
    assert MARKER_START in output and MARKER_END in output


def test_cli_writes_into_the_agent_file_it_finds(project: Path, capsys: pytest.CaptureFixture[str]):
    (project / "AGENTS.md").write_text("# Sample\n", encoding="utf-8")
    assert main(["instructions", "--config", str(project), "--write"]) == 0

    contents = (project / "AGENTS.md").read_text(encoding="utf-8")
    assert contents.startswith("# Sample\n")
    assert MARKER_START in contents
    output = capsys.readouterr().out
    assert "added the codegraph block in AGENTS.md" in output
    # Nothing imports it yet, so Claude Code would not see a word of it.
    assert "reads CLAUDE.md" in output


def test_cli_says_when_it_refreshed_rather_than_added(
    project: Path, capsys: pytest.CaptureFixture[str]
):
    (project / "CLAUDE.md").write_text("# Sample\n", encoding="utf-8")
    main(["instructions", "--config", str(project), "--write"])
    capsys.readouterr()

    assert main(["instructions", "--config", str(project), "--write"]) == 0
    output = capsys.readouterr().out
    assert "refreshed the codegraph block in CLAUDE.md" in output
    assert "reads CLAUDE.md" not in output  # it is CLAUDE.md
    assert (project / "CLAUDE.md").read_text(encoding="utf-8").count(MARKER_START) == 1


def test_cli_never_creates_an_agent_file(project: Path, capsys: pytest.CaptureFixture[str]):
    assert main(["instructions", "--config", str(project), "--write"]) == 2
    assert "no AGENTS.md or CLAUDE.md here" in capsys.readouterr().err
    assert not (project / "AGENTS.md").exists()

    assert main(["instructions", "--config", str(project), "--write", "docs/agents.md"]) == 2
    assert "not a file" in capsys.readouterr().err
    assert not (project / "docs").exists()


def test_cli_writes_the_file_it_is_told_to(project: Path):
    named = project / "notes.md"
    named.write_text("# Notes\n", encoding="utf-8")
    assert main(["instructions", "--config", str(project), "--write", "notes.md"]) == 0
    assert MARKER_START in named.read_text(encoding="utf-8")
