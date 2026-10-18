"""Ground truth may only be read by the eval module. This test keeps that promise."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "replaybook"
READER = "replaybook.eval.ground_truth"
# The generator writes the file; only the reader module reads it.
MAY_NAME_THE_FILE = {
    SRC / "synth" / "generator.py",
    SRC / "eval" / "ground_truth.py",
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_only_eval_imports_the_ground_truth_reader() -> None:
    offenders = []
    for path in SRC.rglob("*.py"):
        if path.is_relative_to(SRC / "eval"):
            continue
        if any(name.startswith(READER) for name in _imports(path)):
            offenders.append(str(path.relative_to(SRC)))
    assert not offenders, offenders


def test_only_generator_and_reader_name_the_ground_truth_file() -> None:
    offenders = []
    for path in SRC.rglob("*.py"):
        if path in MAY_NAME_THE_FILE:
            continue
        if "ground_truth.jsonl" in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(SRC)))
    assert not offenders, offenders
