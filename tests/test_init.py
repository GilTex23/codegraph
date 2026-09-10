"""``codegraph init``: layout detection, client registration and ignores."""

from __future__ import annotations

import ast
import json
import tomllib
from pathlib import Path

import pytest

import codegraph
from codegraph.cli import main
from codegraph.config import load_config
from codegraph.indexer import build
from codegraph.init_project import SERVER_TOOLS, codegraph_executable, detect, init_project


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


def codex_table(root: Path) -> dict:
    data = tomllib.loads((root / ".codex" / "config.toml").read_text(encoding="utf-8"))
    return data["mcp_servers"]["codegraph"]


def claude_entry(root: Path) -> dict:
    data = json.loads((root / ".mcp.json").read_text(encoding="utf-8"))
    return data["mcpServers"]["codegraph"]


def claude_settings(root: Path) -> dict:
    return json.loads((root / ".claude" / "settings.local.json").read_text(encoding="utf-8"))


def registered_tools() -> set[str]:
    """The tools build_server actually exposes, read off the source.

    Parsed rather than imported: this suite must run without the mcp package,
    and the point is to catch a tool added to the server and forgotten here.
    """
    source = Path(codegraph.__file__).parent / "server" / "mcp_server.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and any(
            isinstance(decorator, ast.Call) and getattr(decorator.func, "attr", None) == "tool"
            for decorator in node.decorator_list
        )
    }


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
    result = init_project(root, clients=[])
    assert result.detected.fastapi is False
    config = tomllib.loads(result.config_path.read_text(encoding="utf-8"))
    assert config["bridge"]["enabled"] is False
    assert any("no supported framework" in note for note in result.notes)


def test_a_wordpress_theme_selects_the_wordpress_pack(tmp_path: Path):
    root = tmp_path / "theme"
    (root / "template-parts").mkdir(parents=True)
    (root / "functions.php").write_text(
        "<?php\nadd_action('init', function () {});\n", encoding="utf-8"
    )
    (root / "template-parts" / "hero.php").write_text("<?php\n", encoding="utf-8")

    result = init_project(root, clients=[])
    assert result.detected.wordpress is True
    assert result.detected.framework == "wordpress"
    config = tomllib.loads(result.config_path.read_text(encoding="utf-8"))
    assert config["bridge"]["enabled"] is True
    assert config["bridge"]["backend_framework"] == "wordpress"
    assert config["index"]["languages"] == ["php"]
    # frontend_api_dir belongs to the fastapi pack; here it would be dead weight.
    assert "frontend_api_dir" not in config["bridge"]


def test_the_fastapi_pack_still_gets_its_frontend_directory(fresh: Path):
    result = init_project(fresh, clients=[])
    config = tomllib.loads(result.config_path.read_text(encoding="utf-8"))
    assert config["bridge"]["backend_framework"] == "fastapi"
    assert config["bridge"]["frontend_api_dir"] == "frontend/src/api/"


def test_plain_php_without_wordpress_leaves_the_bridge_off(tmp_path: Path):
    root = tmp_path / "plainphp"
    root.mkdir()
    (root / "lib.php").write_text("<?php\nfunction helper() { return 1; }\n", encoding="utf-8")
    result = init_project(root, clients=[])
    assert result.detected.php_files == 1
    assert result.detected.framework is None


def test_a_flat_project_gets_a_dot_include(tmp_path: Path):
    root = tmp_path / "flat"
    root.mkdir()
    (root / "main.py").write_text("x = 1\n", encoding="utf-8")
    assert detect(root).include == ["."]


# ------------------------------------------------------------- generated config


def test_the_generated_config_is_valid_and_actually_builds(fresh: Path):
    init_project(fresh, clients=[])
    config = load_config(cwd=fresh)
    assert config.index.include == ["backend/", "frontend/src/"]
    assert config.bridge.enabled is True

    summary = build(config, full=True)
    assert summary.files == 4  # node_modules excluded
    assert summary.bridge.endpoints == 0  # the fixture router has no routes yet


def test_an_existing_config_is_only_replaced_with_force(fresh: Path):
    (fresh / ".codegraph.toml").write_text("# mine\n", encoding="utf-8")
    init_project(fresh, clients=[])
    assert (fresh / ".codegraph.toml").read_text(encoding="utf-8") == "# mine\n"

    init_project(fresh, clients=[], force=True)
    assert "codegraph init" in (fresh / ".codegraph.toml").read_text(encoding="utf-8")


# ------------------------------------------------------------- client selection


def test_no_flags_registers_every_client(fresh: Path):
    result = init_project(fresh)
    # Claude takes two files: the server, and the approval that lets it load.
    assert [client.name for client in result.clients] == ["claude", "claude", "codex"]
    assert (fresh / ".mcp.json").exists()
    assert (fresh / ".claude" / "settings.local.json").exists()
    assert (fresh / ".codex" / "config.toml").exists()


def test_claude_only_leaves_codex_alone(fresh: Path):
    init_project(fresh, clients=["claude"])
    assert (fresh / ".mcp.json").exists()
    assert not (fresh / ".codex").exists()


def test_codex_only_leaves_claude_alone(fresh: Path):
    init_project(fresh, clients=["codex"])
    assert (fresh / ".codex" / "config.toml").exists()
    assert not (fresh / ".mcp.json").exists()


def test_no_clients_writes_only_the_config(fresh: Path):
    result = init_project(fresh, clients=[])
    assert result.clients == []
    assert not (fresh / ".mcp.json").exists()
    assert not (fresh / ".codex").exists()


# -------------------------------------------------------------- claude / .mcp.json


def test_mcp_json_pins_the_executable_and_the_config(fresh: Path):
    """A desktop app launches the server with the system PATH; a bare name is not found."""
    init_project(fresh, clients=["claude"])
    entry = claude_entry(fresh)
    assert entry["type"] == "stdio"
    assert entry["command"] == codegraph_executable()
    assert Path(entry["command"]).is_absolute()
    assert entry["args"] == ["serve", "--config", str(fresh / ".codegraph.toml")]
    assert Path(entry["args"][2]).is_file()


def test_an_existing_mcp_entry_is_refreshed_only_with_force(fresh: Path):
    init_project(fresh, clients=["claude"])
    mcp_path = fresh / ".mcp.json"
    stale = json.loads(mcp_path.read_text(encoding="utf-8"))
    stale["mcpServers"]["codegraph"]["command"] = "codegraph"
    mcp_path.write_text(json.dumps(stale), encoding="utf-8")

    init_project(fresh, clients=["claude"])
    assert claude_entry(fresh)["command"] == "codegraph"  # left alone

    init_project(fresh, clients=["claude"], force=True)
    assert claude_entry(fresh)["command"] == codegraph_executable()


def test_a_stale_entry_is_reported_rather_than_silently_kept(fresh: Path):
    """An entry from an older codegraph names a bare command this machine cannot resolve."""
    (fresh / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"codegraph": {"command": "codegraph"}}}), encoding="utf-8"
    )
    result = init_project(fresh, clients=["claude"])
    assert claude_entry(fresh) == {"command": "codegraph"}  # still left alone
    assert any("--force refreshes it" in note for note in result.notes)


# ------------------------------------------------- claude / settings.local.json


def test_the_server_is_approved_up_front(fresh: Path):
    """An unanswered approval prompt looks just like a working setup: no tools, no error."""
    init_project(fresh, clients=["claude"])
    assert claude_settings(fresh)["enabledMcpjsonServers"] == ["codegraph"]


def test_approval_merges_into_existing_settings(fresh: Path):
    settings = fresh / ".claude" / "settings.local.json"
    settings.parent.mkdir()
    settings.write_text(
        json.dumps({"model": "opus", "enabledMcpjsonServers": ["other"]}), encoding="utf-8"
    )
    init_project(fresh, clients=["claude"])
    data = claude_settings(fresh)
    assert data["model"] == "opus"
    assert data["enabledMcpjsonServers"] == ["other", "codegraph"]


def test_a_rejected_server_is_re_enabled_and_reported(fresh: Path):
    settings = fresh / ".claude" / "settings.local.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"disabledMcpjsonServers": ["codegraph"]}), encoding="utf-8")

    result = init_project(fresh, clients=["claude"])
    data = claude_settings(fresh)
    assert data["disabledMcpjsonServers"] == []
    assert data["enabledMcpjsonServers"] == ["codegraph"]
    assert any("re-enabled" in note for note in result.notes)


def test_a_blanket_approval_needs_no_entry(fresh: Path):
    settings = fresh / ".claude" / "settings.local.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"enableAllProjectMcpServers": True}), encoding="utf-8")
    init_project(fresh, clients=["claude"])
    assert claude_settings(fresh) == {"enableAllProjectMcpServers": True}


def test_malformed_settings_are_left_alone(fresh: Path):
    settings = fresh / ".claude" / "settings.local.json"
    settings.parent.mkdir()
    settings.write_text("{ not json", encoding="utf-8")
    result = init_project(fresh, clients=["claude"])
    assert settings.read_text(encoding="utf-8") == "{ not json"
    assert any("settings.local.json could not be parsed" in note for note in result.notes)


def test_mcp_json_merges_into_an_existing_file(fresh: Path):
    (fresh / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"other": {"command": "x"}}}), encoding="utf-8"
    )
    init_project(fresh, clients=["claude"])
    data = json.loads((fresh / ".mcp.json").read_text(encoding="utf-8"))
    assert set(data["mcpServers"]) == {"other", "codegraph"}


def test_a_malformed_mcp_json_is_left_alone(fresh: Path):
    (fresh / ".mcp.json").write_text("{ not json", encoding="utf-8")
    result = init_project(fresh, clients=["claude"])
    assert (fresh / ".mcp.json").read_text(encoding="utf-8") == "{ not json"
    assert any("could not be parsed" in note for note in result.notes)


# --------------------------------------------------------- codex / config.toml


def test_codex_config_has_the_expected_shape(fresh: Path):
    init_project(fresh, clients=["codex"])
    entry = codex_table(fresh)
    assert entry["enabled"] is True
    assert entry["args"] == ["serve", "--config", str(fresh / ".codegraph.toml")]
    assert entry["cwd"] == str(fresh)
    assert entry["startup_timeout_sec"] == 30


def test_codex_paths_are_absolute_and_real(fresh: Path):
    """Codex launches the server without a project context, so nothing may be relative."""
    init_project(fresh, clients=["codex"])
    entry = codex_table(fresh)
    assert Path(entry["command"]).is_absolute()
    assert Path(entry["args"][2]).is_file()
    assert Path(entry["cwd"]).is_dir()


def test_codex_pre_approves_every_tool_the_server_registers(fresh: Path):
    """A tool missing from the table is one Codex stops and asks about, every time."""
    init_project(fresh, clients=["codex"])
    assert set(codex_table(fresh)["tools"]) == set(SERVER_TOOLS) == registered_tools()


def test_the_executable_is_this_codegraph(fresh: Path):
    init_project(fresh, clients=["codex"])
    assert codex_table(fresh)["command"] == codegraph_executable()
    assert Path(codegraph_executable()).name.startswith("codegraph")


def test_windows_backslashes_survive_the_toml_round_trip(fresh: Path):
    init_project(fresh, clients=["codex"])
    raw = (fresh / ".codex" / "config.toml").read_text(encoding="utf-8")
    # Literal strings keep backslashes verbatim; a basic string would eat them.
    assert f"cwd = '{fresh}'" in raw or f'cwd = "{fresh}"'.replace("\\", "\\\\") in raw
    assert codex_table(fresh)["cwd"] == str(fresh)


def test_codex_block_is_appended_to_an_existing_config(fresh: Path):
    codex_path = fresh / ".codex" / "config.toml"
    codex_path.parent.mkdir()
    codex_path.write_text(
        'model = "gpt-5"\n\n[mcp_servers.other]\ncommand = "x"\n', encoding="utf-8", newline="\n"
    )
    init_project(fresh, clients=["codex"])
    data = tomllib.loads(codex_path.read_text(encoding="utf-8"))
    assert data["model"] == "gpt-5"
    assert set(data["mcp_servers"]) == {"other", "codegraph"}


def test_an_existing_codex_entry_is_refreshed_only_with_force(fresh: Path):
    init_project(fresh, clients=["codex"])
    codex_path = fresh / ".codex" / "config.toml"
    stale = codex_path.read_text(encoding="utf-8").replace(
        "startup_timeout_sec = 30", "startup_timeout_sec = 999"
    )
    codex_path.write_text(stale, encoding="utf-8", newline="\n")

    init_project(fresh, clients=["codex"])
    assert codex_table(fresh)["startup_timeout_sec"] == 999  # left alone

    init_project(fresh, clients=["codex"], force=True)
    assert codex_table(fresh)["startup_timeout_sec"] == 30


def test_force_refresh_keeps_neighbouring_tables(fresh: Path):
    init_project(fresh, clients=["codex"])
    codex_path = fresh / ".codex" / "config.toml"
    codex_path.write_text(
        codex_path.read_text(encoding="utf-8") + '\n[mcp_servers.other]\ncommand = "x"\n',
        encoding="utf-8",
        newline="\n",
    )
    init_project(fresh, clients=["codex"], force=True)
    data = tomllib.loads(codex_path.read_text(encoding="utf-8"))
    assert set(data["mcp_servers"]) == {"codegraph", "other"}


def test_a_malformed_codex_config_is_left_alone(fresh: Path):
    codex_path = fresh / ".codex" / "config.toml"
    codex_path.parent.mkdir()
    codex_path.write_text("this is not = = toml\n", encoding="utf-8")
    result = init_project(fresh, clients=["codex"])
    assert codex_path.read_text(encoding="utf-8") == "this is not = = toml\n"
    assert any("not valid TOML" in note for note in result.notes)


# --------------------------------------------------------------------- ignores


def test_gitignore_gains_the_index_and_every_client_file(fresh: Path):
    """All of it pins one machine: the executable that ran init, and one user's approval."""
    result = init_project(fresh)
    assert result.gitignore_added == [
        ".codegraph/",
        ".codex/",
        ".mcp.json",
        ".claude/settings.local.json",
    ]
    contents = (fresh / ".gitignore").read_text(encoding="utf-8")
    assert ".codegraph/" in contents and ".codex/" in contents and ".mcp.json" in contents
    assert contents.startswith("node_modules/\n.env\n")  # existing entries kept


def test_only_the_selected_clients_are_ignored(fresh: Path):
    result = init_project(fresh, clients=["claude"])
    assert result.gitignore_added == [".codegraph/", ".mcp.json", ".claude/settings.local.json"]
    assert ".codex" not in (fresh / ".gitignore").read_text(encoding="utf-8")


def test_only_the_missing_entries_are_added(fresh: Path):
    (fresh / ".gitignore").write_text(".codegraph/\n", encoding="utf-8", newline="\n")
    result = init_project(fresh, clients=["codex"])
    assert result.gitignore_added == [".codex/"]
    assert (fresh / ".gitignore").read_text(encoding="utf-8").count(".codegraph/") == 1


def test_a_directory_line_covers_the_files_beneath_it(fresh: Path):
    """A project ignoring .claude/ needs no second line for the settings file."""
    (fresh / ".gitignore").write_text(".claude/\n", encoding="utf-8", newline="\n")
    result = init_project(fresh, clients=["claude"])
    assert result.gitignore_added == [".codegraph/", ".mcp.json"]
    assert "settings.local.json" not in (fresh / ".gitignore").read_text(encoding="utf-8")


def test_a_negation_does_not_count_as_coverage(fresh: Path):
    (fresh / ".gitignore").write_text("!.mcp.json\n", encoding="utf-8", newline="\n")
    result = init_project(fresh, clients=["claude"])
    assert ".mcp.json" in result.gitignore_added


def test_a_second_run_extends_the_section_instead_of_repeating_the_heading(fresh: Path):
    init_project(fresh, clients=["claude"])
    init_project(fresh, clients=["codex"])
    contents = (fresh / ".gitignore").read_text(encoding="utf-8")
    assert contents.count("# codegraph index") == 1
    assert ".codegraph/" in contents and ".codex/" in contents


def test_gitignore_is_never_created_when_absent(tmp_path: Path):
    root = make_project(tmp_path / "nogit", gitignore=None)
    result = init_project(root)
    assert not (root / ".gitignore").exists()
    assert result.gitignore_added == []
    assert any(".gitignore" in note for note in result.notes)


def test_gitignore_is_left_alone_when_already_covered(fresh: Path):
    covered = ".codegraph/\n.codex/\n.mcp.json\n.claude/\n"
    (fresh / ".gitignore").write_text(covered, encoding="utf-8", newline="\n")
    result = init_project(fresh)
    assert result.gitignore_added == []
    assert (fresh / ".gitignore").read_text(encoding="utf-8") == covered


def test_no_gitignore_flag_is_respected(fresh: Path):
    init_project(fresh, update_gitignore=False)
    assert ".codegraph" not in (fresh / ".gitignore").read_text(encoding="utf-8")


# ------------------------------------------------------------------ idempotency


def test_running_twice_changes_nothing(fresh: Path):
    init_project(fresh)
    watched = (
        fresh / ".codegraph.toml",
        fresh / ".mcp.json",
        fresh / ".claude" / "settings.local.json",
        fresh / ".gitignore",
        fresh / ".codex" / "config.toml",
    )
    before = {path.name: path.read_text(encoding="utf-8") for path in watched}
    second = init_project(fresh)
    after = {path.name: path.read_text(encoding="utf-8") for path in watched}
    assert before == after
    assert second.config_written is False
    assert all(client.already_present for client in second.clients)


# -------------------------------------------------------------------- the CLI


def test_cli_init_reports_what_it_did(fresh: Path, capsys: pytest.CaptureFixture[str]):
    exit_code = main(["init", str(fresh)])
    output = capsys.readouterr().out
    assert exit_code == 0
    assert "include:  backend/, frontend/src/" in output
    assert "wrote .codegraph.toml" in output
    assert "registered for claude in .mcp.json" in output
    assert "approved for claude in .claude/settings.local.json" in output
    assert "registered for codex in .codex/config.toml" in output
    assert (
        "added .codegraph/, .codex/, .mcp.json, .claude/settings.local.json to .gitignore" in output
    )
    assert "next: codegraph build" in output
    assert "restart the agent" in output


@pytest.mark.parametrize(
    ("flags", "claude", "codex"),
    [
        ([], True, True),
        (["--claude"], True, False),
        (["--codex"], False, True),
        (["--claude", "--codex"], True, True),
        (["--no-clients"], False, False),
    ],
)
def test_cli_client_flags_select_targets(fresh: Path, flags: list[str], claude: bool, codex: bool):
    assert main(["init", str(fresh), *flags]) == 0
    assert (fresh / ".mcp.json").exists() is claude
    assert (fresh / ".codex" / "config.toml").exists() is codex


def test_cli_init_rejects_a_missing_directory(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    exit_code = main(["init", str(tmp_path / "nope")])
    assert exit_code == 2
    assert "not a directory" in capsys.readouterr().err
