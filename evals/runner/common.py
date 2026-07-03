"""Corpus loading and workspace materialization shared by both tiers."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CORPUS_DIR = Path(__file__).resolve().parent.parent / "corpus"


@dataclass
class Task:
    id: str
    kind: str  # prove | fix | find_lemma
    prompt: str
    entry_file: str | None
    budget: dict[str, Any]
    check: list[dict[str, Any]]
    path: Path
    tags: list[str] = field(default_factory=list)


def load_corpus(corpus_dir: Path = CORPUS_DIR) -> list[Task]:
    """Load every task.yaml under the corpus directory, sorted by id."""
    tasks: list[Task] = []
    for task_file in sorted(corpus_dir.glob("*/task.yaml")):
        raw = yaml.safe_load(task_file.read_text())
        tasks.append(
            Task(
                id=raw["id"],
                kind=raw["kind"],
                prompt=raw.get("prompt", ""),
                entry_file=raw.get("entry_file"),
                budget=raw.get("budget", {}),
                check=raw.get("check", []),
                path=task_file.parent,
                tags=raw.get("tags", []),
            )
        )
    return tasks


def materialize_workspace(task: Task, dest: Path) -> Path:
    """Copy the task's files/ into *dest* and return the workspace path."""
    dest.mkdir(parents=True, exist_ok=True)
    files_dir = task.path / "files"
    if files_dir.is_dir():
        for entry in files_dir.iterdir():
            if entry.is_file():
                shutil.copy(entry, dest / entry.name)
            else:
                shutil.copytree(entry, dest / entry.name)
    return dest


def substitute_args(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    """Resolve ``{workspace}`` placeholders and ``@file`` content references."""

    def _sub(value: Any) -> Any:
        if isinstance(value, str):
            if value.startswith("@"):
                return (workspace / value[1:]).read_text()
            return value.replace("{workspace}", str(workspace))
        if isinstance(value, dict):
            return {k: _sub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_sub(v) for v in value]
        return value

    return {k: _sub(v) for k, v in args.items()}


def result_dict(call_result: Any) -> dict[str, Any]:
    """Extract the tool's JSON payload from a fastmcp CallToolResult."""
    data = getattr(call_result, "data", None)
    if isinstance(data, dict):
        return data
    structured = getattr(call_result, "structured_content", None)
    if isinstance(structured, dict):
        # FastMCP wraps non-object results as {"result": ...}; tools here
        # always return dicts, so use the structure directly.
        return structured
    for block in getattr(call_result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            return json.loads(text)
    raise AssertionError(f"could not extract a dict payload from {call_result!r}")


def expect_matches(result: dict[str, Any], expect: dict[str, Any]) -> list[str]:
    """Return a list of mismatch descriptions (empty == pass).

    Matching rules per expect key:
      - ``<field>_contains: <substr>`` — substring test on ``str(result[<field>])``
      - anything else — equality against ``result[<field>]``
    """
    problems: list[str] = []
    for key, wanted in expect.items():
        if key.endswith("_contains"):
            base = key[: -len("_contains")]
            haystack = str(result.get(base, ""))
            if str(wanted) not in haystack:
                problems.append(
                    f"{base!r} does not contain {wanted!r} "
                    f"(got: {haystack[:200]!r})"
                )
        elif result.get(key) != wanted:
            problems.append(f"{key!r} == {result.get(key)!r}, expected {wanted!r}")
    return problems
