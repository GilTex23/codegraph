"""``codegraph init``: layout detection and the files it writes."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from codegraph.cli import main
from codegraph.config import load_config
from codegraph.indexer import build
from codegraph.init_project import detect, init_project


def make_project(root: Path, *, gitignore: str | None = None, fastapi: bool = True) -> Path:
    (root / "backend" / "app" / "api").mkdir(parents=True)
    (root / "frontend" / "src" / "api").mkdir(parents=True)
    (root / "frontend" / "src" / "pages").mkdir(parents=True)
    (root / "frontend" / "node_modules" / "pkg").mkdir(parents=True)

    router = (
        'from fastapi import APIRouter\n\nrouter = APIRouter(prefix="/tasks")\n'
        if fastapi
        else "def plain():\n    return 1\n"
    )
    (root / "backend" / "app" / "api" / "tasks.py").write_text(router, encoding="utf-8")
    (root / "backend" / "app" / "main.py").write_text("app = 1\n", encoding="utf-8")
    (root / "frontend" / "src" / "api" / "client.ts").write_text(
        "export const client = {};\n", encoding="utf-8"
    )
    (root / "frontend" / "src" / "pages" / "P.tsx").write_text(
        "export default function P() {\n  return <div />;\n}\n", encoding="utf-8"
    )
    (root / "frontend" / "node_modules" / "pkg" / "index.ts").write_text(
        "export const junk = 1;\n", encoding="utf-8"
    )
    if gitignore is not None:
        (root / ".gitignore").write_text(gitignore, encoding="utf-8", newline="\n")
    return root


@pytest.fixture
def fresh(tmp_path: Path) -> Path:
    return make_project(tmp_path / "app", gitignore="node_modules/\n.env\n")


# ------------------------------------------------------------------ detection


def test_detection_narrows_a_frontend_to_its_src_directory(fresh: Path):
    detected = detect(fresh)
    assert detected.include == ["backend/", "frontend/src/"]
    assert detected.languages == ["python", "typescript"]


def test_a_stray_config_file_does_not_widen_the_frontend_root(fresh: Path):
    """`frontend/vite.config.ts` must not drag the whole package into include."""
    (fresh / "frontend" / "vite.config.ts").write_text("export default {};\n", encoding="utf-8")
    detected = detect(fresh)
    assert detected.include == ["backend/", "frontend/src/"]
    assert detected.frontend_api_dir == "frontend/src/api/"


def test_detection_ignores_node_modules(fresh: Path):
    assert detect(fresh).ts_files == 2  # not the one under node_modules


def test_detection_finds_fastapi_and_the_frontend_api_directory(fresh: Path):
    detected = detect(fresh)
    assert detected.fastapi is True
    assert detected.frontend_api_dir == "frontend/src/api/"


def test_bridge_stays_off_without_fastapi(tmp_path: Path):
    root = make_project(tmp_path / "plain", fastapi=False)
    result = init_project(root, write_mcp=False)
    assert result.detected.fastapi is False
    config = tomllib.loads(result.config_path.read_text(encoding="utf-8"))
    assert config["bridge"]["enabled"] is False
    assert any("no FastAPI" in note for note in result.notes)


def test_a_flat_project_gets_a_dot_include(tmp_path: Path):
    root = tmp_path / "flat"
    root.mkdir()
    (root / "main.py").write_text("x = 1\n", encoding="utf-8")
    assert detect(root).include == ["."]


# ------------------------------------------------------------- generated files


def test_the_generated_config_is_valid_and_actually_builds(fresh: Path):
    init_project(fresh)
    config = load_config(cwd=fresh)
    assert config.index.include == ["backend/", "frontend/src/"]
    assert config.bridge.enabled is True

    summary = build(config, full=True)
    assert summary.files == 4  # node_modules excluded
    assert summary.bridge.endpoints == 0  # the fixture router has no routes yet


def test_gitignore_gains_the_index_directory(fresh: Path):
    result = init_project(fresh)
    assert result.gitignore_updated is True
    contents = (fresh / ".gitignore").read_text(encoding="utf-8")
    assert ".codegraph/" in contents
    assert contents.startswith("node_modules/\n.env\n")  # existing entries kept


def test_gitignore_is_never_created_when_absent(tmp_path: Path):
    root = make_project(tmp_path / "nogit", gitignore=None)
    result = init_project(root)
    assert not (root / ".gitignore").exists()
    assert result.gitignore_updated is False
    assert any(".gitignore" in note for note in result.notes)


def test_gitignore_is_left_alone_when_already_covered(fresh: Path):
    (fresh / ".gitignore").write_text(".codegraph/\n", encoding="utf-8", newline="\n")
    result = init_project(fresh)
    assert result.gitignore_updated is False
    assert (fresh / ".gitignore").read_text(encoding="utf-8") == ".codegraph/\n"


def test_no_gitignore_flag_is_respected(fresh: Path):
    init_project(fresh, update_gitignore=False)
    assert ".codegraph" not in (fresh / ".gitignore").read_text(encoding="utf-8")


def test_mcp_json_uses_a_relative_config_path(fresh: Path):
    """.mcp.json gets committed, so an absolute path would break for everyone else."""
    init_project(fresh)
    data = json.loads((fresh / ".mcp.json").read_text(encoding="utf-8"))
    entry = data["mcpServers"]["codegraph"]
    assert entry["command"] == "codegraph"
    assert entry["args"] == ["serve", "--config", ".codegraph.toml"]


def test_mcp_json_merges_into_an_existing_file(fresh: Path):
    (fresh / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"other": {"command": "x"}}}), encoding="utf-8"
    )
    init_project(fresh)
    data = json.loads((fresh / ".mcp.json").read_text(encoding="utf-8"))
    assert set(data["mcpServers"]) == {"other", "codegraph"}


def test_a_malformed_mcp_json_is_left_alone(fresh: Path):
    (fresh / ".mcp.json").write_text("{ not json", encoding="utf-8")
    result = init_project(fresh)
    assert (fresh / ".mcp.json").read_text(encoding="utf-8") == "{ not json"
    assert any("could not be parsed" in note for note in result.notes)


def test_no_mcp_flag_is_respected(fresh: Path):
    init_project(fresh, write_mcp=False)
    assert not (fresh / ".mcp.json").exists()


# ------------------------------------------------------------------ idempotency


def test_running_twice_changes_nothing(fresh: Path):
    init_project(fresh)
    before = {
        path.name: path.read_text(encoding="utf-8")
        for path in (fresh / ".codegraph.toml", fresh / ".mcp.json", fresh / ".gitignore")
    }
    second = init_project(fresh)
    after = {
        path.name: path.read_text(encoding="utf-8")
        for path in (fresh / ".codegraph.toml", fresh / ".mcp.json", fresh / ".gitignore")
    }
    assert before == after
    assert second.config_written is False


def test_an_existing_config_is_only_replaced_with_force(fresh: Path):
    (fresh / ".codegraph.toml").write_text("# mine\n", encoding="utf-8")
    init_project(fresh)
    assert (fresh / ".codegraph.toml").read_text(encoding="utf-8") == "# mine\n"

    init_project(fresh, force=True)
    assert "codegraph init" in (fresh / ".codegraph.toml").read_text(encoding="utf-8")


# -------------------------------------------------------------------- the CLI


def test_cli_init_reports_what_it_did(fresh: Path, capsys: pytest.CaptureFixture[str]):
    exit_code = main(["init", str(fresh)])
    output = capsys.readouterr().out
    assert exit_code == 0
    assert "include:  backend/, frontend/src/" in output
    assert "wrote .codegraph.toml" in output
    assert "added .codegraph/ to .gitignore" in output
    assert "next: codegraph build" in output


def test_cli_init_rejects_a_missing_directory(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    exit_code = main(["init", str(tmp_path / "nope")])
    assert exit_code == 2
    assert "not a directory" in capsys.readouterr().err
