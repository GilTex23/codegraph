"""Config loading, defaults and human-readable validation errors."""

from __future__ import annotations

from pathlib import Path

import pytest

from codegraph.config import CONFIG_FILENAME, ConfigError, find_config, load_config


def write(root: Path, body: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / CONFIG_FILENAME
    path.write_text(body, encoding="utf-8", newline="\n")
    return path


def test_defaults_apply_when_there_is_no_config(tmp_path: Path):
    config = load_config(cwd=tmp_path)
    assert config.source is None
    assert config.root == tmp_path.resolve()
    assert config.db_path == tmp_path.resolve() / ".codegraph" / "graph.db"
    assert config.index.include == ["."]
    assert config.server.max_results == 50
    assert config.bridge.enabled is False
    assert any("node_modules" in pattern for pattern in config.index.exclude)


def test_config_is_found_by_walking_up(tmp_path: Path):
    write(tmp_path, "[project]\nroot = '.'\n")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert find_config(nested) == tmp_path / CONFIG_FILENAME
    assert load_config(cwd=nested).root == tmp_path.resolve()


def test_paths_are_resolved_relative_to_the_config_file(tmp_path: Path):
    (tmp_path / "repo").mkdir()
    write(tmp_path, "[project]\nroot = 'repo'\ndb_path = 'out/graph.db'\n")
    config = load_config(cwd=tmp_path)
    assert config.root == (tmp_path / "repo").resolve()
    assert config.db_path == (tmp_path / "repo").resolve() / "out" / "graph.db"


def test_explicit_config_path_accepts_a_directory(tmp_path: Path):
    write(tmp_path, "[project]\nroot = '.'\n")
    assert load_config(tmp_path).source == tmp_path / CONFIG_FILENAME


def test_missing_explicit_config_is_an_error(tmp_path: Path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("[project\nroot = '.'\n", "invalid TOML"),
        ("[project]\nroot = 'missing_dir'\n", "missing directory"),
        ("[index]\nlanguages = ['cobol']\n", "unknown language"),
        ("[index]\nmax_file_size_kb = 0\n", "must be positive"),
        ("[index]\nmax_file_size_kb = 'big'\n", "must be an integer"),
        ("[index]\ninclude = 'src'\n", "must be a list of strings"),
        ("[server]\nmax_results = -1\n", "must be positive"),
        ("[server]\nsnippet_max_lines = 0\n", "must be positive"),
        ("[bridge]\nenabled = true\nbackend_framework = 'django'\n", "not supported"),
        ("[bridge]\nenabled = 'yes'\n", "must be true or false"),
        ("[index]\ninclde = ['src']\n", "unknown key"),
        ("[projekt]\nroot = '.'\n", "unknown key"),
    ],
)
def test_bad_config_raises_a_readable_error(tmp_path: Path, body: str, expected: str):
    write(tmp_path, body)
    with pytest.raises(ConfigError, match=expected):
        load_config(cwd=tmp_path)


def test_a_full_config_round_trips(tmp_path: Path):
    write(
        tmp_path,
        "[project]\nroot = '.'\ndb_path = '.codegraph/graph.db'\n\n"
        "[index]\ninclude = ['backend/']\nlanguages = ['python']\n"
        "exclude = ['**/tests/**']\nmax_file_size_kb = 64\n\n"
        "[server]\nmax_results = 10\nsnippet_max_lines = 5\n\n"
        "[bridge]\nenabled = true\nbackend_framework = 'fastapi'\n"
        "frontend_api_dir = 'frontend/src/api/'\n",
    )
    config = load_config(cwd=tmp_path)
    assert config.index.include == ["backend/"]
    assert config.index.languages == ["python"]
    assert config.index.exclude == ["**/tests/**"]
    assert config.index.max_file_size_kb == 64
    assert config.server.max_results == 10
    assert config.server.snippet_max_lines == 5
    assert config.bridge.enabled is True
    assert config.bridge.frontend_api_dir == "frontend/src/api/"
