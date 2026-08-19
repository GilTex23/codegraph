"""Node and edge extraction from Python sources."""

from __future__ import annotations

from codegraph.indexer.python_parser import parse_python

SOURCE = '''\
"""Module doc."""
import os
import os.path as osp
from app.models import Task, User as Person
from .helpers import make_id

MAX_ITEMS: int = 10
lowercase_ignored = 1


def decorator(fn):
    return fn


class Base:
    """Root class."""


class Repo(Base):
    """Repository."""

    @decorator
    async def fetch(self, task_id: int, *, limit: int = 10) -> list[Task]:
        """Fetch tasks."""
        if task_id:
            return self.db.query(Task).all()
        return []


def helper(a, b=os.getcwd()):
    def inner():
        return helper(1)

    return inner()
'''


def parse():
    return parse_python("app/repo.py", SOURCE)


def nodes_by_name(result):
    return {node.name: node for node in result.nodes}


def test_module_node_carries_path_and_docstring():
    result = parse()
    module = result.nodes[0]
    assert module.type == "module"
    assert module.qualified_name == "app/repo.py"
    assert module.docstring == "Module doc."


def test_every_node_type_is_extracted():
    found = nodes_by_name(parse())
    assert found["MAX_ITEMS"].type == "variable"
    assert found["Base"].type == "class"
    assert found["Repo"].type == "class"
    assert found["fetch"].type == "method"
    assert found["helper"].type == "function"
    assert found["inner"].type == "function"
    assert "lowercase_ignored" not in found  # only UPPER_CASE module constants


def test_signatures_keep_annotations_and_defaults():
    found = nodes_by_name(parse())
    assert found["fetch"].signature == (
        "async def fetch(self, task_id: int, *, limit: int=10) -> list[Task]"
    )
    assert found["fetch"].is_async is True
    assert found["Repo"].signature == "class Repo(Base)"
    assert found["MAX_ITEMS"].signature == "MAX_ITEMS: int = 10"


def test_qualified_names_are_scoped():
    found = nodes_by_name(parse())
    assert found["fetch"].qualified_name == "app/repo.py::Repo.fetch"
    assert found["inner"].qualified_name == "app/repo.py::helper.inner"


def test_docstrings_and_parenting():
    result = parse()
    found = nodes_by_name(result)
    assert found["fetch"].docstring == "Fetch tasks."
    assert result.nodes[found["fetch"].parent_local].name == "Repo"


def test_inherits_and_decorates_edges():
    result = parse()
    found = nodes_by_name(result)
    inherits = [e for e in result.edges if e.type == "inherits"]
    assert [(e.src_local, e.dst_name) for e in inherits] == [(found["Repo"].local_id, "Base")]
    decorates = [e for e in result.edges if e.type == "decorates"]
    assert (found["fetch"].local_id, "decorator") in [(e.src_local, e.dst_name) for e in decorates]


def test_calls_cover_name_attribute_and_nesting():
    result = parse()
    found = nodes_by_name(result)
    calls = {(e.src_local, e.dst_name): e for e in result.edges if e.type == "calls"}

    # Attribute call keeps the short name and the whole chain.
    query = calls[(found["fetch"].local_id, "query")]
    assert query.dst_full == "self.db.query"
    # Nested call on the result of another call.
    assert (found["fetch"].local_id, "all") in calls
    # Recursive call is attributed to the innermost enclosing function.
    assert (found["inner"].local_id, "helper") in calls
    assert (found["helper"].local_id, "inner") in calls
    # A call in a default argument belongs to the function it decorates.
    assert (found["helper"].local_id, "getcwd") in calls


def test_calls_inside_an_if_are_recorded_once():
    result = parse()
    found = nodes_by_name(result)
    query_edges = [
        e
        for e in result.edges
        if e.type == "calls" and e.dst_name == "query" and e.src_local == found["fetch"].local_id
    ]
    assert len(query_edges) == 1


def test_imports_are_structured_and_edged():
    result = parse()
    modules = {(imp.module, imp.symbol, imp.alias) for imp in result.imports}
    assert ("os", None, None) in modules
    assert ("os.path", None, "osp") in modules
    assert ("app.models", "Task", None) in modules
    assert ("app.models", "User", "Person") in modules

    relative = next(imp for imp in result.imports if imp.symbol == "make_id")
    assert relative.level == 1 and relative.is_relative

    import_edges = {e.dst_name for e in result.edges if e.type == "imports"}
    assert import_edges == {"os", "os.path", "app.models", ".helpers"}


def test_export_flag_follows_underscore_convention():
    result = parse_python("a.py", "def _hidden():\n    pass\n\n\ndef shown():\n    pass\n")
    found = nodes_by_name(result)
    assert found["_hidden"].is_exported is False
    assert found["shown"].is_exported is True


def test_dunder_all_overrides_export_flag():
    source = '__all__ = ["only"]\n\n\ndef only():\n    pass\n\n\ndef other():\n    pass\n'
    result = parse_python("a.py", source)
    found = nodes_by_name(result)
    assert found["only"].is_exported is True
    assert found["other"].is_exported is False


def test_syntax_error_reports_but_still_yields_a_module_node():
    result = parse_python("bad.py", "def broken(:\n    pass\n")
    assert result.errors
    assert [node.type for node in result.nodes] == ["module"]


def test_decorator_start_line_is_the_declaration_start():
    result = parse_python("a.py", "@deco\ndef f():\n    pass\n")
    function = nodes_by_name(result)["f"]
    assert function.line_start == 1  # so a snippet includes the decorator
