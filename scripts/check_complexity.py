#!/usr/bin/env python3
"""Enforce per-function McCabe cyclomatic complexity for Rust and Python."""

import argparse
import json
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Function:
    path: Path
    name: str
    line: int
    complexity: float


def functions(path: Path, space: dict[str, Any]) -> list[Function]:
    found = []
    children = space.get("spaces", [])
    if space["kind"] == "function":
        # RCA's sum aggregates nested functions/closures. Subtract child spaces
        # so each function is checked independently, not against its subtree sum.
        own_complexity = space["metrics"]["cyclomatic"]["sum"] - sum(
            child["metrics"]["cyclomatic"]["sum"] for child in children
        )
        found.append(Function(path, space["name"], space["start_line"], own_complexity))
    for child in children:
        found.extend(functions(path, child))
    return found


def analyze(path: Path) -> list[Function]:
    result = subprocess.run(
        ["rust-code-analysis-cli", "--metrics", "--output-format", "json", "--paths", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return functions(path, json.loads(result.stdout))


def source_files(paths: list[Path]) -> list[Path]:
    files = set()
    for path in paths:
        if not path.exists():
            raise ValueError(f"missing complexity input: {path}")
        candidates = path.rglob("*") if path.is_dir() else [path]
        files.update(file for file in candidates if file.suffix in {".rs", ".py"} and file.is_file())
    if not files:
        raise ValueError("no Rust/Python inputs found")
    return sorted(files)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, default=list(map(Path, ["src", "tests", "benchmarks", "scripts"])))
    with Path("pyproject.toml").open("rb") as config:
        default_limit = tomllib.load(config)["tool"]["rustybouncer"]["max-cyclomatic-complexity"]
    parser.add_argument("--max-complexity", type=int, default=default_limit)
    args = parser.parse_args()
    if args.max_complexity < 1:
        parser.error("complexity limit must be positive")
    try:
        measured = [function for path in source_files(args.paths) for function in analyze(path)]
        if not measured:
            raise ValueError("analyzer found no functions; refusing an empty check")
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(f"Complexity analysis failed: {error}", file=sys.stderr)
        return 1
    violations = [function for function in measured if function.complexity > args.max_complexity]
    for function in violations:
        print(f"{function.path}:{function.line}: {function.name}: cyclomatic complexity {function.complexity:g} exceeds {args.max_complexity}", file=sys.stderr)
    peak = max(measured, key=lambda function: function.complexity)
    print(f"Complexity: {len(measured)} functions checked; maximum {peak.complexity:g} ({peak.path}:{peak.line}, {peak.name}); limit {args.max_complexity}.")
    return int(bool(violations))


if __name__ == "__main__":
    raise SystemExit(main())
