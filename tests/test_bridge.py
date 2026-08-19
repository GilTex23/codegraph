"""The HTTP bridge: FastAPI routes, prefix stitching, frontend path matching."""

from __future__ import annotations

from pathlib import Path

from codegraph.config import Config, load_config
from codegraph.db import Database
from codegraph.indexer import build
from codegraph.indexer.bridge import _normalize_path
from helpers import edge_rows, find_edge


def endpoints(graph: Database) -> dict[str, dict]:
    rows = graph.conn.execute(
        "SELECT n.qualified_name, n.name, f.path, n.line_start FROM nodes n "
        "JOIN files f ON f.id = n.file_id WHERE n.type = 'endpoint'"
    ).fetchall()
    return {row["qualified_name"]: dict(row) for row in rows}


def test_router_prefix_and_include_router_prefix_are_combined(graph: Database):
    """APIRouter(prefix='/tasks') under include_router(prefix='/api/v1')."""
    assert set(endpoints(graph)) == {"GET /api/v1/tasks/{task_id}", "POST /api/v1/tasks"}


def test_endpoints_live_in_the_router_file(graph: Database):
    endpoint = endpoints(graph)["GET /api/v1/tasks/{task_id}"]
    assert endpoint["path"] == "backend/app/api/tasks.py"
    assert endpoint["line_start"] == 9


def test_handles_edge_points_at_the_handler_function(graph: Database):
    rows = graph.conn.execute(
        "SELECT s.qualified_name AS endpoint, t.qualified_name AS handler FROM edges e "
        "JOIN nodes s ON s.id = e.src_id JOIN nodes t ON t.id = e.dst_id "
        "WHERE e.type = 'handles'"
    ).fetchall()
    mapping = {row["endpoint"]: row["handler"] for row in rows}
    assert mapping["GET /api/v1/tasks/{task_id}"] == "backend/app/api/tasks.py::get_task"
    assert mapping["POST /api/v1/tasks"] == "backend/app/api/tasks.py::create_task"


def test_template_parameter_matches_a_path_parameter(graph: Database):
    """`/api/v1/tasks/${id}` on the frontend must match `/{task_id}`."""
    edge = find_edge(edge_rows(graph, "calls_api"), "fetchTask", "GET /api/v1/tasks/${id}")
    assert edge["resolved"] == 1
    assert edge["confidence"] == "exact"
    assert edge["dst_node_name"] == "/api/v1/tasks/{task_id}"


def test_calls_api_edge_starts_at_the_enclosing_function(graph: Database):
    rows = edge_rows(graph, "calls_api")
    assert {row["src_name"] for row in rows} == {"fetchTask", "createTask"}
    assert all(row["src_path"] == "frontend/src/api/tasks.ts" for row in rows)


def test_unmatched_frontend_path_still_produces_an_edge(project: Path):
    api_file = project / "frontend" / "src" / "api" / "tasks.ts"
    api_file.write_text(
        api_file.read_text(encoding="utf-8") + "\nexport async function ghost() {\n"
        "  return client.get('/nothing/here');\n}\n",
        encoding="utf-8",
        newline="\n",
    )
    config = load_config(cwd=project)
    build(config, full=True)
    with Database.open_readonly(config.db_path) as graph:
        edge = find_edge(edge_rows(graph, "calls_api"), "ghost", "GET /nothing/here")
    assert edge["resolved"] == 0
    assert edge["dst_name"].endswith("/nothing/here")


def test_disabled_bridge_skips_the_whole_pass(project: Path):
    toml = project / ".codegraph.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8").replace("enabled = true", "enabled = false"),
        encoding="utf-8",
        newline="\n",
    )
    config = load_config(cwd=project)
    summary = build(config, full=True)
    assert summary.bridge.endpoints == 0
    with Database.open_readonly(config.db_path) as graph:
        assert endpoints(graph) == {}
        assert edge_rows(graph, "handles") == []
        assert edge_rows(graph, "calls_api") == []


def test_rebuilding_does_not_duplicate_endpoints(config: Config, summary):
    build(config)  # incremental second pass over unchanged files
    with Database.open_readonly(config.db_path) as graph:
        assert len(endpoints(graph)) == 2
        assert len(edge_rows(graph, "handles")) == 2


def test_aliased_router_import_still_gets_its_prefix(project: Path):
    """`from app.api import tasks as tasks_router` must still match."""
    main = project / "backend" / "app" / "main.py"
    main.write_text(
        "from fastapi import FastAPI\n\n"
        "from app.api import tasks as tasks_router\n\n"
        "app = FastAPI()\n"
        'app.include_router(tasks_router.router, prefix="/api/v1")\n',
        encoding="utf-8",
        newline="\n",
    )
    config = load_config(cwd=project)
    build(config, full=True)
    with Database.open_readonly(config.db_path) as graph:
        assert set(endpoints(graph)) == {"GET /api/v1/tasks/{task_id}", "POST /api/v1/tasks"}


def test_baseurl_prefix_is_matched_by_trailing_segments(project: Path):
    """A frontend path without the client's baseURL still finds its endpoint.

    `/tasks` must reach `/api/v1/tasks` and not be confused by a longer route
    that merely ends the same way.
    """
    api_file = project / "frontend" / "src" / "api" / "tasks.ts"
    api_file.write_text(
        "import { client } from './client';\n\n"
        "export async function listTasks() {\n"
        "  return client.get('/tasks');\n"
        "}\n",
        encoding="utf-8",
        newline="\n",
    )
    router = project / "backend" / "app" / "api" / "tasks.py"
    router.write_text(
        router.read_text(encoding="utf-8")
        + '\n\n@router.get("")\nasync def list_all():\n    return []\n'
        + '\n\n@router.get("/archive/tasks")\nasync def decoy():\n    return []\n',
        encoding="utf-8",
        newline="\n",
    )
    config = load_config(cwd=project)
    build(config, full=True)
    with Database.open_readonly(config.db_path) as graph:
        edge = find_edge(edge_rows(graph, "calls_api"), "listTasks", "GET /tasks")
    assert edge["resolved"] == 1
    assert edge["confidence"] == "heuristic"
    assert edge["dst_node_name"] == "/api/v1/tasks"


def test_path_normalisation_is_style_agnostic():
    assert _normalize_path("/tasks/{task_id}") == "/tasks/:param"
    assert _normalize_path("/tasks/${id}") == "/tasks/:param"
    assert _normalize_path("/tasks/:id") == "/tasks/:param"
    assert _normalize_path("/tasks/") == "/tasks"
    assert _normalize_path("/tasks//sub") == "/tasks/sub"
    assert _normalize_path("/") == "/"
