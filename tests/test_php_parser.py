"""PHP extraction, WordPress wiring, and PHP-specific resolution."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from codegraph.config import Config, load_config
from codegraph.db import Database
from codegraph.indexer import build
from codegraph.indexer.php_parser import parse_php
from helpers import edge_rows, find_edge

FIXTURES = Path(__file__).parent / "fixtures"

SOURCE = """<?php
namespace App\\Theme;

use App\\Support\\Helper;

require_once Z52_DIR . '/inc/helpers.php';

define('Z52_VERSION', '1.0.0');
const MAX_ITEMS = 10;

/**
 * Does a thing.
 */
function z52_asset_url(string $relative, ?int $post_id = null): string {
    $file = z52_slugify($relative);
    return Helper::slug($file);
}

add_action('wp_head', function (): void {
    wp_enqueue_style('z52');
    z52_asset_url('style.css');
});

class Repo extends Base implements Countable {
    public function load(int $id): void {
        $this->fetch($id);
        new Helper();
    }
    private function hidden(): void {}
}

interface Countable { public function count(): int; }
trait Loggable { public function log(): void {} }
enum Status: string { case Open = 'open'; }
"""


def parse():
    return parse_php("functions.php", SOURCE)


def nodes_by_name(result):
    return {node.name: node for node in result.nodes if node.type != "module"}


@pytest.fixture
def theme(tmp_path: Path) -> Path:
    destination = tmp_path / "theme"
    shutil.copytree(FIXTURES / "theme", destination, ignore=shutil.ignore_patterns(".codegraph"))
    return destination


@pytest.fixture
def theme_graph(theme: Path):
    config = load_config(cwd=theme)
    build(config, full=True)
    database = Database.open_readonly(config.db_path)
    yield database
    database.close()


# ------------------------------------------------------------------ structure


def test_every_declaration_kind_is_extracted():
    found = nodes_by_name(parse())
    assert found["z52_asset_url"].type == "function"
    assert found["Repo"].type == "class"
    assert found["load"].type == "method"
    assert found["Countable"].type == "interface"
    assert found["Loggable"].type == "trait"
    assert found["Status"].type == "enum"
    assert found["MAX_ITEMS"].type == "variable"
    assert found["Z52_VERSION"].type == "variable"  # define() declares one too


def test_signatures_keep_types_and_defaults():
    found = nodes_by_name(parse())
    assert found["z52_asset_url"].signature == (
        "function z52_asset_url(string $relative, ?int $post_id = null): string"
    )
    assert "public" in found["load"].signature


def test_docblocks_become_docstrings():
    assert nodes_by_name(parse())["z52_asset_url"].docstring == "Does a thing."


def test_visibility_drives_the_export_flag():
    """PHP has no export list, so `private` is the closest real signal."""
    found = nodes_by_name(parse())
    assert found["load"].is_exported is True
    assert found["hidden"].is_exported is False
    assert found["z52_asset_url"].is_exported is True


def test_heritage_edges():
    result = parse()
    found = nodes_by_name(result)
    kinds = {(e.type, e.dst_name) for e in result.edges if e.src_local == found["Repo"].local_id}
    assert ("inherits", "Base") in kinds
    assert ("implements", "Countable") in kinds


def test_calls_cover_every_call_shape():
    result = parse()
    found = nodes_by_name(result)
    calls = {(e.src_local, e.dst_name): e for e in result.edges if e.type == "calls"}
    assert (found["z52_asset_url"].local_id, "z52_slugify") in calls  # plain
    assert calls[(found["z52_asset_url"].local_id, "slug")].dst_full == "Helper.slug"  # static
    assert calls[(found["load"].local_id, "fetch")].dst_full == "$this.fetch"  # member
    assert (found["load"].local_id, "Helper") in calls  # new Helper()


def test_require_keeps_the_literal_path():
    """The constant prefix is unknowable here; the tail is what can be matched."""
    result = parse()
    modules = {imp.module for imp in result.imports}
    assert "/inc/helpers.php" in modules
    assert "App\\Support" in modules


# ---------------------------------------------------------- closure callbacks


def test_a_hook_closure_becomes_a_named_declaration():
    """Theme logic lives in these; without a node the whole block is invisible."""
    found = nodes_by_name(parse())
    assert "add_action(wp_head)" in found
    assert found["add_action(wp_head)"].type == "function"
    assert found["add_action(wp_head)"].is_exported is False


def test_calls_inside_a_closure_belong_to_it_not_the_file():
    result = parse()
    found = nodes_by_name(result)
    owner = found["add_action(wp_head)"].local_id
    owned = {e.dst_name for e in result.edges if e.src_local == owner}
    assert {"wp_enqueue_style", "z52_asset_url"} <= owned


def test_a_closure_is_not_named_after_a_string_inside_its_own_body():
    """The hint must come from the arguments before the callback, not from it."""
    result = parse_php(
        "a.php",
        "<?php\narray_map(function ($x) { return trim(' | '); }, $list);\n",
    )
    names = [node.name for node in result.nodes if node.type != "module"]
    assert names == ["array_map@2"]


def test_inline_arrow_functions_do_not_become_nodes():
    result = parse_php("a.php", "<?php\n$double = array_map(fn($x) => $x * 2, $list);\n")
    assert [node.type for node in result.nodes] == ["module"]


def test_a_broken_file_reports_errors_without_raising():
    result = parse_php("bad.php", "<?php\nfunction broken( { ;;;\n")
    assert result.errors
    assert result.nodes[0].type == "module"


# ------------------------------------------------------------------ resolving


def test_a_unique_global_function_resolves_exactly(theme_graph: Database):
    """PHP has one global namespace, so a unique name is not a guess."""
    edge = find_edge(edge_rows(theme_graph, "calls"), "z52_asset_url", "z52_slugify")
    assert edge["resolved"] == 1
    assert edge["confidence"] == "exact"
    assert edge["dst_path"] == "inc/helpers.php"


def test_a_require_resolves_by_path_suffix(theme_graph: Database):
    edge = find_edge(edge_rows(theme_graph, "imports"), "functions", "/inc/helpers.php")
    assert edge["resolved"] == 1
    assert edge["dst_path"] == "inc/helpers.php"


def test_undeclared_bare_calls_are_third_party_not_unknown(theme_graph: Database):
    """WordPress core is not in the graph, but it is not a gap either."""
    row = theme_graph.conn.execute(
        "SELECT confidence, resolved FROM edges WHERE type = 'calls' AND dst_name = 'get_header'"
    ).fetchone()
    assert row["resolved"] == 0
    assert row["confidence"] == "external"


def test_php_builtins_are_dropped(theme_graph: Database):
    remaining = theme_graph.conn.execute(
        "SELECT count(*) FROM edges WHERE dst_name = 'strtolower'"
    ).fetchone()[0]
    assert remaining == 0


# --------------------------------------------------------- the WordPress pack


def hooks(graph: Database) -> dict[str, str | None]:
    rows = graph.conn.execute(
        "SELECT n.qualified_name, t.name AS target FROM nodes n "
        "LEFT JOIN edges e ON e.src_id = n.id AND e.type = 'handles' "
        "LEFT JOIN nodes t ON t.id = e.dst_id WHERE n.type IN ('hook', 'endpoint')"
    ).fetchall()
    return {row["qualified_name"]: row["target"] for row in rows}


def test_hooks_become_nodes(theme_graph: Database):
    registered = hooks(theme_graph)
    assert "action wp_enqueue_scripts" in registered
    assert "filter body_class" in registered


def test_a_closure_callback_is_linked_to_its_hook(theme_graph: Database):
    assert hooks(theme_graph)["action wp_enqueue_scripts"] == "add_action(wp_enqueue_scripts)"


def test_a_named_callback_is_linked_to_its_function(theme_graph: Database):
    assert hooks(theme_graph)["filter body_class"] == "z52_body_class"


def test_wp_ajax_hooks_become_endpoints(theme_graph: Database):
    """An AJAX action is an HTTP endpoint wearing a hook's name."""
    registered = hooks(theme_graph)
    assert registered["AJAX z52_search"] == "z52_ajax_search"
    assert "action wp_ajax_z52_search" not in registered  # not counted twice


def test_template_parts_link_to_the_files_they_pull_in(theme_graph: Database):
    rendered = {row["dst_name"]: row["dst_path"] for row in edge_rows(theme_graph, "renders")}
    assert rendered["template-parts/hero"] == "template-parts/hero.php"
    assert rendered["template-parts/card-wide"] == "template-parts/card-wide.php"
    assert rendered["header"] == "header.php"
    assert rendered["footer"] == "footer.php"


def test_an_unmatched_template_part_still_records_the_slug(theme_graph: Database):
    edge = find_edge(edge_rows(theme_graph, "renders"), "page", "template-parts/missing")
    assert edge["resolved"] == 0
    assert edge["dst_name"] == "template-parts/missing"


def test_the_bridge_is_reported_and_idempotent(theme: Path):
    config: Config = load_config(cwd=theme)
    first = build(config, full=True)
    assert first.bridge.hooks >= 2
    assert first.bridge.templates_matched == 4
    assert "hooks" in first.render()

    build(config)  # incremental second pass must not duplicate the pack's output
    with Database.open_readonly(config.db_path) as graph:
        assert len(hooks(graph)) == len(hooks(graph))
        assert (
            graph.conn.execute(
                "SELECT count(*) FROM nodes WHERE type IN ('hook', 'endpoint')"
            ).fetchone()[0]
            == first.bridge.hooks + first.bridge.endpoints
        )
