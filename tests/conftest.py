"""Shared fixtures.

The sample project is copied into ``tmp_path`` before every build so tests
never write into the repository, and so path handling is exercised on whatever
filesystem the tests run on.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from codegraph.config import Config, load_config
from codegraph.db import Database
from codegraph.indexer import BuildSummary, build

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A throwaway copy of the sample backend+frontend project."""
    destination = tmp_path / "project"
    shutil.copytree(
        FIXTURES / "sample",
        destination,
        ignore=shutil.ignore_patterns(".codegraph"),
    )
    return destination


@pytest.fixture
def config(project: Path) -> Config:
    return load_config(cwd=project)


@pytest.fixture
def summary(config: Config) -> BuildSummary:
    return build(config, full=True)


@pytest.fixture
def graph(config: Config, summary: BuildSummary) -> Database:
    database = Database.open_readonly(config.db_path)
    yield database
    database.close()
