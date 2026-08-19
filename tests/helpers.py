"""Assertion helpers shared by the graph tests."""

from __future__ import annotations

from codegraph.db import Database


def edge_rows(database: Database, edge_type: str) -> list[dict]:
    """Every edge of one type, flattened with source/target paths and names."""
    rows = database.conn.execute(
        "SELECT sf.path AS src_path, s.name AS src_name, e.dst_name, e.resolved, "
        "e.confidence, tf.path AS dst_path, t.name AS dst_node_name, t.type AS dst_type "
        "FROM edges e "
        "JOIN nodes s ON s.id = e.src_id JOIN files sf ON sf.id = s.file_id "
        "LEFT JOIN nodes t ON t.id = e.dst_id LEFT JOIN files tf ON tf.id = t.file_id "
        "WHERE e.type = ?",
        (edge_type,),
    ).fetchall()
    return [dict(row) for row in rows]


def find_edge(rows: list[dict], src_name: str, dst_name: str) -> dict:
    for row in rows:
        if row["src_name"] == src_name and row["dst_name"] == dst_name:
            return row
    raise AssertionError(f"no edge {src_name} -> {dst_name} in {rows}")
