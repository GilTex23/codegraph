"""File discovery, ignore patterns and size limits."""

from __future__ import annotations

from pathlib import Path

from codegraph.config import DEFAULT_EXCLUDE
from codegraph.indexer.walker import compile_ignore, is_ignored, rel_posix, walk


def matches(pattern: str, path: str) -> bool:
    return is_ignored(path, compile_ignore([pattern]))


def test_glob_patterns_behave_like_gitignore():
    assert matches("**/node_modules/**", "frontend/node_modules/react/index.js")
    assert matches("**/node_modules/**", "node_modules/x.ts")
    assert matches("**/*.d.ts", "src/types.d.ts")
    assert matches("**/*.d.ts", "types.d.ts")
    assert not matches("**/*.d.ts", "src/types.ts")
    assert matches(".venv/", ".venv/lib/x.py")  # leading dot survives
    assert matches("build/", "build/out.js")
    assert not matches("*.py", "pkg/mod.py")  # a single star does not cross '/'
    assert matches("**/migrations/**", "app/db/migrations/0001.py")


def test_a_single_file_can_be_excluded_by_name(tmp_path: Path):
    """A build script beside the code is not part of the code."""
    (tmp_path / "inc").mkdir()
    (tmp_path / "deploy.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("y = 2\n", encoding="utf-8")
    (tmp_path / "inc" / "deploy.py").write_text("z = 3\n", encoding="utf-8")

    found, _ = walk(tmp_path, ["."], ["python"], ["deploy.py"])
    assert [file.path for file in found] == ["app.py", "inc/deploy.py"]

    found, _ = walk(tmp_path, ["."], ["python"], ["**/deploy.py"])
    assert [file.path for file in found] == ["app.py"]


def test_rel_posix_never_emits_a_backslash(tmp_path: Path):
    nested = tmp_path / "a" / "b" / "c.py"
    nested.parent.mkdir(parents=True)
    nested.touch()
    assert rel_posix(nested, tmp_path) == "a/b/c.py"


def test_walk_respects_include_language_and_exclude(tmp_path: Path):
    (tmp_path / "backend").mkdir()
    (tmp_path / "frontend" / "src").mkdir(parents=True)
    (tmp_path / "frontend" / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "docs").mkdir()

    (tmp_path / "backend" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "frontend" / "src" / "a.ts").write_text("export const a = 1;\n", encoding="utf-8")
    (tmp_path / "frontend" / "src" / "b.tsx").write_text("export const b = 1;\n", encoding="utf-8")
    (tmp_path / "frontend" / "node_modules" / "pkg" / "i.ts").write_text("//\n", encoding="utf-8")
    (tmp_path / "docs" / "notes.py").write_text("y = 1\n", encoding="utf-8")

    found, skipped = walk(
        root=tmp_path,
        include=["backend/", "frontend/src/"],
        languages=["python", "typescript"],
        exclude=list(DEFAULT_EXCLUDE),
    )
    assert [file.path for file in found] == [
        "backend/app.py",
        "frontend/src/a.ts",
        "frontend/src/b.tsx",
    ]
    assert skipped == []


def test_language_selection_filters_extensions(tmp_path: Path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "b.ts").write_text("export const b = 1;\n", encoding="utf-8")
    found, _ = walk(tmp_path, ["."], ["python"], list(DEFAULT_EXCLUDE))
    assert [file.path for file in found] == ["a.py"]


def test_tsx_files_get_their_own_language_tag(tmp_path: Path):
    (tmp_path / "c.tsx").write_text("export const c = 1;\n", encoding="utf-8")
    found, _ = walk(tmp_path, ["."], ["typescript"], [])
    assert found[0].language == "tsx"


def test_oversized_files_are_skipped_with_a_reason(tmp_path: Path):
    (tmp_path / "big.py").write_text("# " + "x" * 5000 + "\n", encoding="utf-8")
    found, skipped = walk(tmp_path, ["."], ["python"], [], max_file_size_kb=1)
    assert found == []
    assert len(skipped) == 1
    assert "max_file_size_kb" in skipped[0].reason


def test_content_is_newline_normalised(tmp_path: Path):
    (tmp_path / "crlf.py").write_bytes(b"a = 1\r\nb = 2\r\n")
    found, _ = walk(tmp_path, ["."], ["python"], [])
    assert "\r" not in found[0].content
    assert found[0].size_bytes == len(b"a = 1\nb = 2\n")
