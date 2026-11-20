"""Offline, one-query diagnostic on the same snapshots as agent_eval."""

import argparse
import json
import re
import statistics
import subprocess
import time
from pathlib import Path

from agent_eval import cbm_env
from puretiktoken import get_encoding

from codegraph.config import load_config
from codegraph.server.tools import GraphTools
from codegraph.tokens import fit_lines, token_count


def rg_evidence(root, anchor):
    result = subprocess.run(
        [
            "rg",
            "--color=never",
            "--sort=path",
            "--path-separator=/",
            "-inF",
            "-C",
            "2",
            "--max-columns",
            "350",
            "--",
            anchor,
            ".",
        ],
        cwd=root,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr)
    return result.stdout.rstrip() or "No matches"


def cbm_evidence(binary, directory, project, anchor, callers):
    if callers:
        pattern = json.dumps(anchor)
        query = (
            f"MATCH (a)-[:CALLS]->(b) WHERE b.name = {pattern} "
            "RETURN a.name,a.file_path,b.file_path LIMIT 50"
        )
        tool, args = "query_graph", {"query": query}
    else:
        tool, args = "search_graph", {"name_pattern": "(?i)" + re.escape(anchor), "limit": 30}
    result = subprocess.run(
        [binary, "cli", tool],
        input=json.dumps(dict(args, project=project)),
        cwd=directory / "source",
        env=cbm_env(directory),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    data = json.loads(result.stdout)
    if "error" in data:
        raise RuntimeError(data["error"])
    return result.stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("snapshots", type=Path)
    parser.add_argument("--cbm", type=Path, required=True)
    parser.add_argument("--max-tokens", type=int, default=1200)
    args = parser.parse_args()
    projects = json.loads(args.manifest.read_text(encoding="utf-8"))
    results = []
    for name, spec in projects.items():
        directory = args.snapshots.resolve() / name
        snapshot = json.loads((directory / "snapshot.json").read_text(encoding="utf-8"))
        project = snapshot["cbm_project"]
        graph = GraphTools(load_config(directory / "graph.toml"))
        try:
            for case in spec["cases"]:
                anchor = case["anchor"]
                for mode in ("rg", "codegraph", "cbm"):
                    start = time.perf_counter()
                    if mode == "rg":
                        raw = rg_evidence(directory / "source", anchor)
                    elif mode == "cbm":
                        raw = cbm_evidence(
                            args.cbm.resolve(),
                            directory,
                            project,
                            anchor,
                            case["intent"] == "callers",
                        )
                    else:
                        raw = graph.context(case["question"] + f" (`{anchor}`)", args.max_tokens)
                    # CLI JSON is a single line. Keep a token prefix so partial rows
                    # still contribute evidence, following a terminal output cap.
                    encoding = get_encoding("o200k_base")
                    ids = encoding.encode_ordinary(raw)
                    evidence = raw
                    if len(ids) > args.max_tokens:
                        evidence = encoding.decode(ids[: args.max_tokens - 1]) + "…"
                    evidence = fit_lines(evidence, args.max_tokens)
                    normalized = evidence.replace("\\", "/").casefold()
                    missing = [s for s in case["required"] if s.casefold() not in normalized]
                    results.append(
                        dict(
                            project=name,
                            case=case["id"],
                            mode=mode,
                            missing=missing,
                            tokens=token_count(evidence),
                            evidence=evidence,
                            seconds=round(time.perf_counter() - start, 3),
                        )
                    )
        finally:
            graph.close()
    path = args.snapshots / "retrieval.json"
    path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    for mode in ("rg", "codegraph", "cbm"):
        rows = [r for r in results if r["mode"] == mode]
        print(
            mode,
            "complete",
            sum(not r["missing"] for r in rows),
            "/",
            len(rows),
            "median payload tokens",
            statistics.median(r["tokens"] for r in rows),
        )


if __name__ == "__main__":
    main()
