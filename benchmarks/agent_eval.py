"""Compare fresh Codex sessions on pinned source snapshots and a private JSON case file.

Each project has root, revision, include (git pathspecs), and cases. Each case
has id, question and required (answer fragments). An optional source_commit
records the change which motivated the question. See README for usage.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

from codegraph.config import load_config
from codegraph.indexer import build, source_fingerprint

MODES = ("rg", "codegraph", "cbm")


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args])


def prepare(spec, directory, competitor):
    """Export only committed source files. Keep indexes outside the searchable tree."""
    root = Path(spec["root"]).resolve()
    revision = git(root, "rev-parse", "--verify", spec["revision"] + "^{commit}").decode().strip()
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / "source"
    source.mkdir(exist_ok=True)
    git(source, "init", "--quiet")
    archive = git(root, "archive", "--format=zip", revision, "--", *spec["include"])
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        for member in z.infolist():
            path = (source / member.filename).resolve()
            if not path.is_relative_to(source) or member.external_attr >> 28 == 10:
                continue
            if path.suffix in (".py", ".ts", ".tsx", ".php"):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(z.read(member))

    config_path = directory / "graph.toml"
    config_path.write_text(
        "[project]\nroot = 'source'\ndb_path = '../graph.db'\n"
        f"[bridge]\nenabled = {str(bool(spec.get('framework'))).lower()}\n"
        f"backend_framework = {json.dumps(spec.get('framework', 'fastapi'))}\n",
        encoding="utf-8",
    )
    config = load_config(config_path)
    summary = build(config)
    options = cbm_env(directory)
    proc = subprocess.run(
        [competitor, "cli", "index_repository"],
        cwd=source,
        env=options,
        check=True,
        input=json.dumps({"repo_path": str(source)}),
        capture_output=True,
        encoding="utf-8",
    )
    report = json.loads(proc.stdout)
    if not summary.files or not report.get("coverage", {}).get("files"):
        raise RuntimeError(f"An empty index would invalidate this trial: {report}")
    manifest = {"revision": revision, "files": summary.files, "cbm_project": report["project"]}
    (directory / "snapshot.json").write_text(json.dumps(manifest), encoding="utf-8")


def cbm_env(directory):
    return dict(
        os.environ,
        CBM_CACHE_DIR=str(directory / "cbm-cache"),
        CBM_ALLOWED_ROOT=str(directory / "source"),
        CBM_WORKERS="2",
    )


def trial(directory, case, mode, model, competitor):
    source = directory / "source"
    options = [
        "--ignore-user-config",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--json",
        "--model",
        model,
        "--cd",
        str(source),
    ]
    guidance = "Use rg and targeted source reads."
    if mode != "rg":
        if mode == "codegraph":
            command = sys.executable
            args = ["-m", "codegraph.cli", "serve", "--config", str(directory / "graph.toml")]
            names = [
                "context",
                "get_callers",
                "get_callees",
                "get_definition",
                "get_neighbors",
                "get_file_outline",
                "get_directory_outline",
                "search_symbol",
                "trace_endpoint",
            ]
            env = {}
        else:
            command, args = str(competitor), []
            names = [
                "get_file_outline",
                "search_graph",
                "search_code",
                "trace_path",
                "query_graph",
                "get_code_snippet",
                "list_projects",
            ]
            env = {k: v for k, v in cbm_env(directory).items() if k.startswith("CBM_")}
        for key, value in (("command", command), ("args", args), ("enabled_tools", names)):
            options += ["-c", f"mcp_servers.graph.{key}={json.dumps(value)}"]
        for key, value in env.items():
            options += ["-c", f"mcp_servers.graph.env.{key}={json.dumps(value)}"]
        guidance = "Try the graph MCP first. Use rg or targeted reads as needed."
    prompt = (
        f"Answer this code navigation question using only this source tree. {guidance} "
        "Inspect code or graph evidence. No edits or network. Return only the requested "
        "names and repository-relative paths, concisely.\n" + case["question"]
    )
    start = time.perf_counter()
    try:
        proc = subprocess.run(
            [shutil.which("codex") or "codex", "exec", *options, "-"],
            input=prompt,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=240,
            cwd=source,
        )
    except subprocess.TimeoutExpired:
        return {"error": "timeout", "seconds": time.perf_counter() - start}
    events = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]
    items = [e["item"] for e in events if e.get("type") == "item.completed"]
    usage = next((e["usage"] for e in events if e.get("type") == "turn.completed"), None)
    if proc.returncode or usage is None:
        error = next((e for e in reversed(events) if "error" in e or "message" in e), None)
        return {"error": str(error or proc.stderr[-800:]), "seconds": time.perf_counter() - start}
    answer = "\n".join(i["text"] for i in items if i["type"] == "agent_message")
    normalized = answer.replace("\\", "/").casefold()
    missing = [s for s in case["required"] if s.casefold() not in normalized]
    calls = [i for i in items if i["type"] in ("command_execution", "mcp_tool_call")]
    return {
        "answer": answer,
        "missing": missing,
        "passed": not missing,
        "usage": usage,
        "seconds": round(time.perf_counter() - start, 2),
        "calls": len(calls),
        "graph_calls": sum(
            i["type"] == "mcp_tool_call" and i.get("server") == "graph" for i in calls
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--cbm", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    projects = json.loads(args.manifest.read_text(encoding="utf-8"))
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    competitor = args.cbm.resolve(strict=True)
    fingerprint = hashlib.sha256(args.manifest.read_bytes()).hexdigest()
    settings = {
        "manifest": fingerprint,
        "model": args.model,
        "codegraph": source_fingerprint(),
        "competitor": hashlib.sha256(competitor.read_bytes()).hexdigest(),
    }
    settings_path = output / "settings.json"
    previous_settings = (
        json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
    )
    if previous_settings and previous_settings != settings and (
        (output / "results.jsonl").exists() or any(
            previous_settings.get(k) != settings[k] for k in ("manifest", "model", "competitor")
        )
    ):
        parser.error("use a new output directory for different inputs or tool versions")
    for name, spec in projects.items():
        directory = output / name
        if not (directory / "snapshot.json").exists():
            prepare(spec, directory, competitor)
        elif previous_settings.get("codegraph") != settings["codegraph"]:
            build(load_config(directory / "graph.toml"), full=True)
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    if args.prepare_only:
        return
    results = output / "results.jsonl"
    previous = results.read_text(encoding="utf-8").splitlines() if results.exists() else []
    done = {
        (r["project"], r["case"], r["mode"]) for r in map(json.loads, previous) if "error" not in r
    }
    count = 0
    for name, spec in projects.items():
        for index, case in enumerate(spec["cases"]):
            for offset in range(len(MODES)):
                mode = MODES[(index + offset) % len(MODES)]
                if (name, case["id"], mode) in done:
                    continue
                row = trial(output / name, case, mode, args.model, competitor)
                row.update(project=name, case=case["id"], mode=mode)
                with results.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                print(name, case["id"], mode, row.get("passed", row.get("error")), flush=True)
                if "error" in row:
                    raise SystemExit(1)
                count += 1
                if count == args.limit:
                    return


if __name__ == "__main__":
    main()
