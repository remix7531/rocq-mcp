"""Post-hoc, transcript-independent grading.

Grading never trusts what a runner (scripted or agent) *claims* happened:
it re-opens a fresh MCP client session and executes the task's ``check``
tool calls against the final workspace state.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evals.runner.common import Task, expect_matches, result_dict, substitute_args


@dataclass
class GradeReport:
    task_id: str
    passed: bool
    failures: list[str]


async def grade(task: Task, workspace: Path, client: Any) -> GradeReport:
    """Run every ``check`` entry through *client* and collect mismatches.

    *client* is an already-connected ``fastmcp.Client`` (fresh session —
    do not reuse the one the scenario ran on when grading agent runs).
    """
    failures: list[str] = []
    for i, check in enumerate(task.check):
        tool = check["tool"]
        args = substitute_args(check.get("args", {}), workspace)
        call = await client.call_tool(tool, args)
        result = result_dict(call)
        for problem in expect_matches(result, check.get("expect", {})):
            failures.append(f"check[{i}] {tool}: {problem}")
    return GradeReport(task_id=task.id, passed=not failures, failures=failures)
