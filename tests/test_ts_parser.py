"""Node and edge extraction from TypeScript and TSX sources."""

from __future__ import annotations

from codegraph.indexer.ts_parser import parse_typescript

TS_SOURCE = """\
import React, { useState } from 'react';
import type { Task } from '../types';
import * as api from './api';
export { Helper } from './helper';
export * from './models';

export const API_URL = '/api';

/** Does a thing. */
export function doThing(a: number, b: string = 'x'): Promise<void> {
  return client.get(`/tasks/${a}`);
}

export const arrowFn = async (x: number): Promise<Task> => {
  const y = helper(x);
  return y;
};

export default class Repo extends Base implements IRepo {
  private items: Task[] = [];

  async load(id: number): Promise<void> {
    this.items = await api.fetchAll(id);
  }

  handler = (event: Event) => {
    this.load(1);
  };
}

export interface IRepo {
  load(id: number): Promise<void>;
}

export type Maybe<T> = T | null;

export enum Color {
  Red,
}

function internalOnly() {
  return new Repo();
}
"""

TSX_SOURCE = """\
import { useEffect, useState } from 'react';
import { fetchTask } from '../api';

/** Lists tasks. */
export default function TaskList() {
  const [task, setTask] = useState(null);
  useEffect(() => {
    fetchTask(1).then(setTask);
  }, []);
  return <div>{task}</div>;
}

export const Small = () => <span>hi</span>;

function notAComponent() {
  return 42;
}
"""


def nodes_by_name(result):
    return {node.name: node for node in result.nodes if node.type != "module"}


def parse_ts():
    return parse_typescript("src/repo.ts", TS_SOURCE, "typescript")


def parse_tsx():
    return parse_typescript("src/pages/TaskList.tsx", TSX_SOURCE, "tsx")


def test_module_node_uses_the_file_path():
    module = parse_ts().nodes[0]
    assert module.type == "module"
    assert module.qualified_name == "src/repo.ts"


def test_every_declaration_kind_is_extracted():
    found = nodes_by_name(parse_ts())
    assert found["doThing"].type == "function"
    assert found["arrowFn"].type == "function"
    assert found["Repo"].type == "class"
    assert found["load"].type == "method"
    assert found["handler"].type == "method"  # class field holding an arrow
    assert found["IRepo"].type == "interface"
    assert found["Maybe"].type == "type_alias"
    assert found["Color"].type == "enum"
    assert found["API_URL"].type == "variable"


def test_signatures_and_async_flags():
    found = nodes_by_name(parse_ts())
    assert found["doThing"].signature == (
        "function doThing(a: number, b: string = 'x'): Promise<void>"
    )
    assert found["arrowFn"].is_async is True
    assert found["load"].is_async is True
    assert found["Repo"].signature == "class Repo extends Base implements IRepo"


def test_qualified_names_are_scoped_to_the_class():
    found = nodes_by_name(parse_ts())
    assert found["load"].qualified_name == "src/repo.ts::Repo.load"


def test_export_detection():
    found = nodes_by_name(parse_ts())
    assert found["doThing"].is_exported is True
    assert found["internalOnly"].is_exported is False


def test_jsdoc_before_an_exported_declaration():
    found = nodes_by_name(parse_ts())
    assert found["doThing"].docstring == "Does a thing."


def test_heritage_edges():
    result = parse_ts()
    found = nodes_by_name(result)
    kinds = {(e.type, e.dst_name) for e in result.edges if e.src_local == found["Repo"].local_id}
    assert ("inherits", "Base") in kinds
    assert ("implements", "IRepo") in kinds


def test_imports_keep_the_raw_module_path():
    result = parse_ts()
    modules = {(imp.module, imp.symbol, imp.alias) for imp in result.imports}
    assert ("react", "default", "React") in modules
    assert ("react", "useState", None) in modules
    assert ("../types", "Task", None) in modules
    assert ("./api", None, "api") in modules

    reexports = {(imp.module, imp.symbol) for imp in result.imports if imp.is_reexport}
    assert ("./helper", "Helper") in reexports
    assert ("./models", "*") in reexports

    edges = {e.dst_name for e in result.edges if e.type == "imports"}
    assert edges == {"react", "../types", "./api", "./helper", "./models"}


def test_calls_including_new_expressions_and_member_chains():
    result = parse_ts()
    found = nodes_by_name(result)
    calls = {(e.src_local, e.dst_name): e for e in result.edges if e.type == "calls"}
    assert calls[(found["doThing"].local_id, "get")].dst_full == "client.get"
    assert calls[(found["load"].local_id, "fetchAll")].dst_full == "api.fetchAll"
    assert (found["internalOnly"].local_id, "Repo") in calls  # new Repo()


def test_tsx_functions_returning_jsx_are_components():
    found = nodes_by_name(parse_tsx())
    assert found["TaskList"].type == "component"
    assert found["Small"].type == "component"
    assert found["notAComponent"].type == "function"
    assert found["TaskList"].docstring == "Lists tasks."


def test_calls_in_anonymous_callbacks_belong_to_the_component():
    result = parse_tsx()
    found = nodes_by_name(result)
    owned = {e.dst_name for e in result.edges if e.src_local == found["TaskList"].local_id}
    assert {"useState", "useEffect", "fetchTask", "then"} <= owned


def test_a_broken_file_reports_errors_without_raising():
    result = parse_typescript("src/bad.ts", "export function broken( { const ;;;\n", "typescript")
    assert result.errors
    assert result.nodes[0].type == "module"


# ---------------------------------------------------- grammar-defect recovery

IN_MEMBER_SOURCE = """type Dashboard = {
  work_time: Array<{
    machine_name: string
    total_hours: number
    in_progress_hours: number
    done_hours: number
  }>
}

export function readDashboard(): Dashboard {
  return load();
}
"""


def test_object_type_member_starting_with_in_is_recovered():
    """tree-sitter-typescript reads `in` as an operator when `;` is missing.

    Without repair the whole rest of the type -- and everything the grammar
    could not resync on -- is lost.  `in_progress` is far too common a name for
    that to be acceptable.
    """
    result = parse_typescript("src/dash.ts", IN_MEMBER_SOURCE, "typescript")
    assert result.errors == []
    found = nodes_by_name(result)
    assert found["Dashboard"].type == "type_alias"
    assert found["readDashboard"].type == "function"


def test_repair_preserves_line_numbers_and_names():
    result = parse_typescript("src/dash.ts", IN_MEMBER_SOURCE, "typescript")
    found = nodes_by_name(result)
    assert found["Dashboard"].line_start == 1
    assert found["readDashboard"].line_start == 10
    assert "in_progress_hours" in found["Dashboard"].signature


def test_several_such_members_in_a_row_are_all_recovered():
    source = (
        "interface I {\n"
        "  x: number\n"
        "  in_a: number\n"
        "  in_b: number\n"
        "  in_c: number\n"
        "  y: string\n"
        "}\n"
    )
    assert parse_typescript("src/i.ts", source, "typescript").errors == []


def test_a_genuinely_broken_file_is_not_silently_repaired():
    result = parse_typescript("src/bad.ts", "export function broken( { const ;;;\n", "typescript")
    assert result.errors
