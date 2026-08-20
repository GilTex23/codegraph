"""Every MCP tool, against the fixture graph."""

from __future__ import annotations

import asyncio
import shutil
import subprocess

import pytest

from codegraph.config import Config
from codegraph.server.mcp_server import build_server
from codegraph.server.tools import GraphTools, _normalize_domain, split_identifier

EXPECTED_TOOLS = {
    "search_symbol",
    "get_definition",
    "get_callers",
    "get_callees",
    "get_file_outline",
    "get_directory_outline",
    "get_change_impact",
    "get_imports",
    "get_neighbors",
    "get_project_overview",
    "get_domain_slice",
    "trace_endpoint",
}


@pytest.fixture
def tools(config: Config, summary) -> GraphTools:
    instance = GraphTools(config)
    yield instance
    instance.close()


# ------------------------------------------------------------------- searching


def test_search_symbol_finds_by_name(tools: GraphTools):
    output = tools.search_symbol("TaskService")
    assert "TaskService" in output
    assert "backend/app/services/task_service.py:4" in output


def test_search_symbol_matches_camel_case_prefixes(tools: GraphTools):
    """`fetch` has to find `fetchTask`, which the tokenizer keeps whole."""
    assert "fetchTask" in tools.search_symbol("fetch")


def test_search_symbol_filters_by_type(tools: GraphTools):
    output = tools.search_symbol("task", type="interface")
    assert "interface" in output
    assert "class" not in output


def test_search_symbol_never_returns_bodies(tools: GraphTools):
    assert "return" not in tools.search_symbol("task")


def test_search_symbol_reports_a_miss_plainly(tools: GraphTools):
    assert "no symbol matches" in tools.search_symbol("definitelyNotHere")


def test_search_respects_max_results(config: Config, summary):
    config.server.max_results = 2
    instance = GraphTools(config)
    try:
        lines = instance.search_symbol("task").splitlines()
    finally:
        instance.close()
    assert len(lines) == 3  # two results plus the truncation note
    assert "truncated" in lines[-1]


# ----------------------------------------------------------------- definitions


def test_get_definition_includes_short_source(tools: GraphTools):
    output = tools.get_definition("complete")
    assert "backend/app/models/task.py::Task.complete" in output
    assert "Mark the task done." in output
    assert "self.save()" in output


def test_get_definition_lists_ambiguous_names(tools: GraphTools):
    output = tools.get_definition("Task")
    assert "2 definitions" in output
    assert "backend/app/models/task.py::Task" in output
    assert "frontend/src/types/task.ts::Task" in output


def test_get_definition_accepts_a_qualified_name(tools: GraphTools):
    output = tools.get_definition("backend/app/models/task.py::Task")
    assert "[class]" in output


def test_get_definition_omits_long_bodies(config: Config, summary):
    config.server.snippet_max_lines = 2
    instance = GraphTools(config)
    try:
        output = instance.get_definition("complete")
    finally:
        instance.close()
    assert "body omitted" in output
    assert "self.save()" not in output


def test_get_definition_reports_a_miss(tools: GraphTools):
    assert "no definition found" in tools.get_definition("nope")


# ----------------------------------------------------------------------- edges


def test_get_callers_lists_call_sites_and_flags_heuristics(tools: GraphTools):
    output = tools.get_callers("list_tasks")
    assert "get_task" in output
    assert "backend/app/api/tasks.py:" in output
    assert "[heuristic]" in output


def test_get_callers_mentions_unresolved_mentions(tools: GraphTools):
    assert "unresolved call site" in tools.get_callers("useState")


def test_get_callees(tools: GraphTools):
    output = tools.get_callees("get_task")
    assert "list_tasks" in output


def test_get_callees_shows_unresolved_targets(tools: GraphTools):
    assert "unresolved" in tools.get_callees("TaskList")


# ----------------------------------------------------------------------- files


def test_get_file_outline_is_a_body_free_structure(tools: GraphTools):
    output = tools.get_file_outline("backend/app/models/task.py")
    assert "class Task(Base)" in output
    assert "def complete(self) -> None" in output
    assert "self.save()" not in output


def test_get_file_outline_accepts_a_partial_path(tools: GraphTools):
    assert "class Task(Base)" in tools.get_file_outline("models/task.py")


def test_get_file_outline_reports_unknown_files(tools: GraphTools):
    assert "not indexed" in tools.get_file_outline("nowhere/x.py")


def test_get_imports_shows_both_directions(tools: GraphTools):
    output = tools.get_imports("frontend/src/api/tasks.ts")
    assert "./client" in output
    assert "frontend/src/api/client.ts" in output
    assert "frontend/src/api/index.ts" in output  # imported by the barrel


def test_get_imports_marks_reexports(tools: GraphTools):
    assert "[re-export]" in tools.get_imports("frontend/src/types/index.ts")


# ---------------------------------------------------------------- directories


def test_get_directory_outline_summarises_each_file(tools: GraphTools):
    output = tools.get_directory_outline("backend/app/models")
    assert "backend/app/models/  3 files" in output
    assert "task.py" in output and "Task" in output
    assert "base.py" in output and "Base" in output


def test_get_directory_outline_is_cheaper_than_the_file_outlines(tools: GraphTools):
    """It answers "which file", so it must cost far less than opening them."""
    directory = tools.get_directory_outline("backend/app/models")
    files = "".join(
        tools.get_file_outline(f"backend/app/models/{name}.py") for name in ("base", "task")
    )
    assert len(directory) < len(files)
    assert "def complete" not in directory  # no signatures at this zoom level


def test_get_directory_outline_prefers_exports_then_internals(tools: GraphTools):
    output = tools.get_directory_outline("backend/app/services")
    assert "TaskService" in output


def test_get_directory_outline_accepts_a_trailing_slash(tools: GraphTools):
    assert tools.get_directory_outline("backend/app/models/") == tools.get_directory_outline(
        "backend/app/models"
    )


def test_get_directory_outline_reports_an_unknown_directory(tools: GraphTools):
    assert "no indexed files" in tools.get_directory_outline("nowhere/at/all")


# --------------------------------------------------------------- change impact


def test_change_impact_falls_back_to_the_graph_without_git(config: Config, summary):
    """A project need not be a git repository for the tool to be useful."""
    target = config.root / "backend" / "app" / "models" / "task.py"
    target.write_text(
        target.read_text(encoding="utf-8").replace("def complete", "def finish"),
        encoding="utf-8",
        newline="\n",
    )
    instance = GraphTools(config)
    try:
        output = instance.get_change_impact()
    finally:
        instance.close()
    assert "no git here" in output
    assert "backend/app/models/task.py" in output


def test_change_impact_says_so_when_nothing_changed(config: Config, summary):
    instance = GraphTools(config)
    try:
        output = instance.get_change_impact()
    finally:
        instance.close()
    assert "no changes detected" in output


def test_change_impact_survives_a_missing_git_binary(config: Config, summary, monkeypatch):
    """The tool must degrade, not raise, where git is not installed."""
    import codegraph.server.tools as tools_module

    def no_git(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(tools_module.subprocess, "run", no_git)
    instance = GraphTools(config)
    try:
        output = instance.get_change_impact()
    finally:
        instance.close()
    assert "no git here" in output


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_change_impact_reads_the_git_working_tree(config: Config, summary):
    root = config.root
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"],
        cwd=root,
        check=True,
    )
    target = root / "backend" / "app" / "services" / "task_service.py"
    target.write_text(
        target.read_text(encoding="utf-8").replace("return [Task()]", "return [Task(), Task()]"),
        encoding="utf-8",
        newline="\n",
    )

    instance = GraphTools(config)
    try:
        output = instance.get_change_impact()
    finally:
        instance.close()
    assert "git working tree" in output
    assert "task_service.py" in output
    assert "list_tasks" in output
    assert "<- get_task" in output  # the blast radius, which is the point


# ------------------------------------------------------------------- neighbors


def test_get_neighbors_shows_both_directions(tools: GraphTools):
    output = tools.get_neighbors("Task", depth=1)
    assert "-> inherits Base" in output
    assert "<- calls list_tasks" in output


def test_get_neighbors_caps_depth_at_two(tools: GraphTools):
    deep = tools.get_neighbors("Task", depth=9)
    assert deep == tools.get_neighbors("Task", depth=2)


# -------------------------------------------------------------------- overview


def test_get_project_overview_lists_directories_and_symbols(tools: GraphTools):
    output = tools.get_project_overview()
    assert "backend/app/models/" in output
    assert "TaskService" in output
    assert "19 files" in output


# ---------------------------------------------------------------- domain slice


def test_get_domain_slice_groups_by_layer(tools: GraphTools):
    output = tools.get_domain_slice("task")
    for layer in ("[model]", "[schema]", "[api]", "[frontend]"):
        assert layer in output
    assert "backend/app/models/task.py" in output
    assert "backend/app/schemas/task.py" in output
    assert "frontend/src/api/tasks.ts" in output


def test_get_domain_slice_normalises_naming_styles(tools: GraphTools):
    def body(spelling: str) -> str:
        return tools.get_domain_slice(spelling).split("\n", 1)[1]

    baseline = body("task")
    for spelling in ("Tasks", "TASKS", "tasks"):
        assert body(spelling) == baseline


def test_get_domain_slice_reports_a_miss(tools: GraphTools):
    assert "nothing matches" in tools.get_domain_slice("invoice")


def test_domain_normalisation_rules():
    assert _normalize_domain("DesignRequests") == "designrequest"
    assert _normalize_domain("design_request") == "designrequest"
    assert _normalize_domain("designRequests") == "designrequest"
    assert _normalize_domain("categories") == "category"
    assert _normalize_domain("design requests") == "designrequest"
    assert split_identifier("fetchTaskList") == ["fetch", "task", "list"]
    assert split_identifier("HTTPServer") == ["http", "server"]


# ------------------------------------------------------------------- endpoints


def test_trace_endpoint_covers_the_whole_route(tools: GraphTools):
    output = tools.trace_endpoint("/tasks/{task_id}")
    assert "GET /api/v1/tasks/{task_id}" in output
    assert "handler: get_task" in output
    assert "-> method list_tasks" in output
    assert "<- frontend fetchTask" in output


def test_trace_endpoint_accepts_a_handler_name(tools: GraphTools):
    assert "handler: create_task" in tools.trace_endpoint("create_task")


def test_trace_endpoint_without_a_bridge_says_so(project, tmp_path):
    from codegraph.config import load_config
    from codegraph.indexer import build

    toml = project / ".codegraph.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8").replace("enabled = true", "enabled = false"),
        encoding="utf-8",
        newline="\n",
    )
    config = load_config(cwd=project)
    build(config, full=True)
    instance = GraphTools(config)
    try:
        output = instance.trace_endpoint("get_task")
    finally:
        instance.close()
    assert "backend-only view" in output
    assert "get_task" in output


# ------------------------------------------------------------- the MCP surface


def test_all_tools_are_registered_with_descriptions(config: Config, summary):
    server = build_server(config)
    listed = server.list_tools()
    if asyncio.iscoroutine(listed):
        listed = asyncio.run(listed)
    assert {tool.name for tool in listed} == EXPECTED_TOOLS
    for tool in listed:
        assert tool.description and len(tool.description) > 40


def test_tools_are_callable_through_the_server(config: Config, summary):
    server = build_server(config)

    async def call(name: str, arguments: dict) -> str:
        result = await server.call_tool(name, arguments)
        return "\n".join(getattr(block, "text", "") for block in result.content)

    assert "backend/app/models/" in asyncio.run(call("get_project_overview", {}))
    assert "[model]" in asyncio.run(call("get_domain_slice", {"domain": "tasks"}))
    assert "TaskService" in asyncio.run(call("search_symbol", {"query": "TaskService"}))
