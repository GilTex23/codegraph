"""SQLite schema and data-access layer.

Design notes worth knowing before touching this file:

* ``edges.dst_id`` is ``ON DELETE SET NULL``, deliberately *not* ``CASCADE``.
  With ``CASCADE``, re-indexing file B would delete edges that point *into* B
  from an unchanged file A, and since A is never re-parsed those edges would be
  gone for good -- the graph would silently rot with every incremental build.
  Resolution is recomputed from scratch on every build instead, so a nulled
  ``dst_id`` costs nothing.
* ``imports`` is an extra table the resolver needs.  Import *edges* are still
  written to ``edges`` as the graph model requires, but edges cannot carry the
  structure (module / symbol / alias / relative level) needed to follow
  re-export chains, and that structure must survive incremental builds where
  the importing file is skipped.
* ``nodes_fts`` uses ``prefix='2 3'`` so ``parseConfig`` is reachable from the
  query ``parse`` -- the default tokenizer splits ``snake_case`` but keeps
  ``camelCase`` as a single token.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .models import ParseResult

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    language TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    indexed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    parent_id INTEGER REFERENCES nodes(id) ON DELETE CASCADE,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    signature TEXT,
    docstring TEXT,
    is_exported INTEGER NOT NULL DEFAULT 1,
    is_async INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY,
    src_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    dst_id INTEGER REFERENCES nodes(id) ON DELETE SET NULL,
    dst_name TEXT,
    dst_full TEXT,
    type TEXT NOT NULL,
    line INTEGER,
    resolved INTEGER NOT NULL DEFAULT 0,
    confidence TEXT NOT NULL DEFAULT 'exact'
);

CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    module TEXT NOT NULL,
    symbol TEXT,
    alias TEXT,
    line INTEGER,
    level INTEGER NOT NULL DEFAULT 0,
    is_relative INTEGER NOT NULL DEFAULT 0,
    is_reexport INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name);
CREATE INDEX IF NOT EXISTS idx_nodes_file ON nodes(file_id);
CREATE INDEX IF NOT EXISTS idx_nodes_qname ON nodes(qualified_name);
CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(type);
CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes(parent_id);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_id);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_id);
CREATE INDEX IF NOT EXISTS idx_edges_dstname ON edges(dst_name);
CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(type);
CREATE INDEX IF NOT EXISTS idx_imports_file ON imports(file_id);
CREATE INDEX IF NOT EXISTS idx_imports_module ON imports(module);

CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
    name, qualified_name, docstring, signature,
    content='nodes', content_rowid='id', prefix='2 3'
);

CREATE TRIGGER IF NOT EXISTS nodes_fts_ai AFTER INSERT ON nodes BEGIN
    INSERT INTO nodes_fts(rowid, name, qualified_name, docstring, signature)
    VALUES (new.id, new.name, new.qualified_name, new.docstring, new.signature);
END;

CREATE TRIGGER IF NOT EXISTS nodes_fts_ad AFTER DELETE ON nodes BEGIN
    INSERT INTO nodes_fts(nodes_fts, rowid, name, qualified_name, docstring, signature)
    VALUES ('delete', old.id, old.name, old.qualified_name, old.docstring, old.signature);
END;

CREATE TRIGGER IF NOT EXISTS nodes_fts_au AFTER UPDATE ON nodes BEGIN
    INSERT INTO nodes_fts(nodes_fts, rowid, name, qualified_name, docstring, signature)
    VALUES ('delete', old.id, old.name, old.qualified_name, old.docstring, old.signature);
    INSERT INTO nodes_fts(rowid, name, qualified_name, docstring, signature)
    VALUES (new.id, new.name, new.qualified_name, new.docstring, new.signature);
END;
"""


# Dropped in dependency order: triggers and the FTS index first, then the
# children, then the tables they point at.
RESET_SQL = """
DROP TRIGGER IF EXISTS nodes_fts_ai;
DROP TRIGGER IF EXISTS nodes_fts_ad;
DROP TRIGGER IF EXISTS nodes_fts_au;
DROP TABLE IF EXISTS nodes_fts;
DROP TABLE IF EXISTS edges;
DROP TABLE IF EXISTS imports;
DROP TABLE IF EXISTS nodes;
DROP TABLE IF EXISTS files;
DROP TABLE IF EXISTS meta;
"""


class SchemaVersionError(Exception):
    """Existing database was built by a different schema version."""


@dataclass(slots=True)
class FileRow:
    id: int
    path: str
    language: str
    content_hash: str
    size_bytes: int
    indexed_at: str


def _utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Database:
    """Thin wrapper over a ``sqlite3.Connection`` holding the graph."""

    def __init__(self, conn: sqlite3.Connection, path: Path | None = None) -> None:
        self.conn = conn
        self.path = path
        self.conn.row_factory = sqlite3.Row

    # ------------------------------------------------------------------ open

    @classmethod
    def open(cls, path: Path, *, reset: bool = False) -> Database:
        """Open (creating if needed) a writable graph database.

        ``reset`` empties the graph by dropping its objects rather than by
        deleting the file.  On Windows an open file cannot be unlinked, and an
        MCP server serving this project holds the database open read-only for
        as long as the agent session lives -- so deleting it made a full
        rebuild fail exactly when the tool was in use.  SQLite is happy to drop
        and recreate underneath a reader, which sees the old snapshot until the
        rebuild commits and the new one afterwards.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        db = cls(conn, path)
        db._configure()
        if reset:
            db._drop_schema()
        db._ensure_schema()
        return db

    @classmethod
    def open_readonly(cls, path: Path) -> Database:
        """Open an existing graph read-only, as every MCP tool does."""
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(
                f"Graph database not found at {path}. Run 'codegraph build' first."
            )
        uri = f"file:{path.resolve().as_posix()}?mode=ro"
        # The MCP runtime dispatches sync tool functions onto worker threads, so
        # the connection must not be pinned to its creating thread.  Access is
        # read-only and serialised by a lock in GraphTools.
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        db = cls(conn, path)
        conn.execute("PRAGMA foreign_keys = ON")
        db._check_version()
        return db

    def _configure(self) -> None:
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")

    def _drop_schema(self) -> None:
        """Empty the graph in place.  Falls back to the file only if that fails."""
        try:
            self.conn.executescript(RESET_SQL)
            self.conn.execute("PRAGMA user_version = 0")
            self.conn.commit()
            return
        except sqlite3.DatabaseError:
            pass  # unreadable or corrupt: there is nothing to drop cleanly

        self.conn.close()
        if self.path is not None:
            self.path.unlink(missing_ok=True)
            self.conn = sqlite3.connect(self.path)
            self.conn.row_factory = sqlite3.Row
            self._configure()

    def _ensure_schema(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        has_tables = self.conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='nodes'"
        ).fetchone()[0]
        if has_tables and version != SCHEMA_VERSION:
            raise SchemaVersionError(
                f"Graph at {self.path} uses schema version {version}, "
                f"this codegraph expects {SCHEMA_VERSION}. "
                f"Rebuild it from scratch: codegraph build --full"
            )
        self.conn.executescript(SCHEMA_SQL)
        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.conn.commit()

    def _check_version(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            raise SchemaVersionError(
                f"Graph at {self.path} uses schema version {version}, "
                f"this codegraph expects {SCHEMA_VERSION}. "
                f"Rebuild it: codegraph build --full"
            )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
        except Exception:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

    # ------------------------------------------------------------------ meta

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # ----------------------------------------------------------------- files

    def all_files(self) -> dict[str, FileRow]:
        rows = self.conn.execute(
            "SELECT id, path, language, content_hash, size_bytes, indexed_at FROM files"
        ).fetchall()
        return {row["path"]: FileRow(**dict(row)) for row in rows}

    def get_file(self, path: str) -> FileRow | None:
        row = self.conn.execute(
            "SELECT id, path, language, content_hash, size_bytes, indexed_at "
            "FROM files WHERE path = ?",
            (path,),
        ).fetchone()
        return FileRow(**dict(row)) if row else None

    def upsert_file(self, path: str, language: str, content_hash: str, size_bytes: int) -> int:
        """Insert or update a file row and return its id, wiping its old contents."""
        existing = self.get_file(path)
        if existing is None:
            cur = self.conn.execute(
                "INSERT INTO files(path, language, content_hash, size_bytes, indexed_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (path, language, content_hash, size_bytes, _utcnow()),
            )
            return int(cur.lastrowid)
        self.conn.execute(
            "UPDATE files SET language = ?, content_hash = ?, size_bytes = ?, indexed_at = ? "
            "WHERE id = ?",
            (language, content_hash, size_bytes, _utcnow(), existing.id),
        )
        self.clear_file_contents(existing.id)
        return existing.id

    def clear_file_contents(self, file_id: int) -> None:
        """Drop everything derived from one file, keeping the file row itself."""
        self.conn.execute(
            "DELETE FROM edges WHERE src_id IN (SELECT id FROM nodes WHERE file_id = ?)",
            (file_id,),
        )
        self.conn.execute("DELETE FROM imports WHERE file_id = ?", (file_id,))
        # Explicit, so the FTS delete trigger fires on plain (non-cascading) deletes.
        self.conn.execute("DELETE FROM nodes WHERE file_id = ?", (file_id,))

    def delete_files(self, paths: Iterable[str]) -> int:
        count = 0
        for path in paths:
            row = self.get_file(path)
            if row is None:
                continue
            self.clear_file_contents(row.id)
            self.conn.execute("DELETE FROM files WHERE id = ?", (row.id,))
            count += 1
        return count

    # ----------------------------------------------------------- parse output

    def write_parse_result(self, file_id: int, result: ParseResult) -> dict[int, int]:
        """Persist one file's nodes/edges/imports; returns local_id -> row id."""
        id_map: dict[int, int] = {}
        for node in result.nodes:
            parent_id = id_map.get(node.parent_local) if node.parent_local is not None else None
            cur = self.conn.execute(
                "INSERT INTO nodes(file_id, type, name, qualified_name, parent_id, "
                "line_start, line_end, signature, docstring, is_exported, is_async) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    file_id,
                    node.type,
                    node.name,
                    node.qualified_name,
                    parent_id,
                    node.line_start,
                    node.line_end,
                    node.signature,
                    node.docstring,
                    int(node.is_exported),
                    int(node.is_async),
                ),
            )
            id_map[node.local_id] = int(cur.lastrowid)

        self.conn.executemany(
            "INSERT INTO edges(src_id, dst_id, dst_name, dst_full, type, line, "
            "resolved, confidence) VALUES (?, NULL, ?, ?, ?, ?, 0, 'exact')",
            [
                (
                    id_map[edge.src_local],
                    edge.dst_name,
                    edge.dst_full,
                    edge.type,
                    edge.line,
                )
                for edge in result.edges
                if edge.src_local in id_map
            ],
        )

        self.conn.executemany(
            "INSERT INTO imports(file_id, module, symbol, alias, line, level, "
            "is_relative, is_reexport) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    file_id,
                    imp.module,
                    imp.symbol,
                    imp.alias,
                    imp.line,
                    imp.level,
                    int(imp.is_relative),
                    int(imp.is_reexport),
                )
                for imp in result.imports
            ],
        )
        return id_map

    # ------------------------------------------------------------- statistics

    def counts(self) -> dict[str, int]:
        q = self.conn.execute
        return {
            "files": q("SELECT count(*) FROM files").fetchone()[0],
            "nodes": q("SELECT count(*) FROM nodes").fetchone()[0],
            "edges": q("SELECT count(*) FROM edges").fetchone()[0],
            "unresolved_edges": q("SELECT count(*) FROM edges WHERE resolved = 0").fetchone()[0],
            "external_edges": q(
                "SELECT count(*) FROM edges WHERE resolved = 0 AND confidence = 'external'"
            ).fetchone()[0],
        }

    def optimize(self) -> None:
        self.conn.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('optimize')")
        self.conn.commit()
        self.conn.execute("ANALYZE")
        self.conn.commit()
