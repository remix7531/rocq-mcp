"""Tier-1 eval harness — scripted tool sequences over the real toolchain.

Runs each corpus task's fixed scenario through an in-memory
``fastmcp.Client`` (full MCP serialization path), then grades the final
workspace on a FRESH client session via the task's ``check`` block.

These are the regression gate for tool-surface changes: a renamed
parameter, a changed failure ``reason``, or a broken envelope fails here
against the real coqc + pet toolchain (available in CI; skipped locally
when absent).
"""

from __future__ import annotations

import pytest
from evals.runner import scenarios
from evals.runner.common import load_corpus, materialize_workspace
from evals.runner.grade import grade

from tests.conftest import COQC_AVAILABLE, PET_AVAILABLE

pytestmark = [
    pytest.mark.skipif(
        not (COQC_AVAILABLE and PET_AVAILABLE),
        reason="tier-1 evals need both coqc and pet",
    ),
    # Real toolchain + per-task pet spawn: more than the global 120s.
    pytest.mark.timeout(300),
]

TASKS = load_corpus()


def test_every_task_has_a_scenario():
    missing = [t.id for t in TASKS if t.id not in scenarios.SCENARIOS]
    assert not missing, f"corpus tasks without a tier-1 scenario: {missing}"
    orphaned = [tid for tid in scenarios.SCENARIOS if tid not in {t.id for t in TASKS}]
    assert not orphaned, f"scenarios without a corpus task: {orphaned}"


@pytest.mark.parametrize("task", TASKS, ids=[t.id for t in TASKS])
async def test_tier1(task, tmp_path):
    from fastmcp import Client

    from rocq_mcp.server import mcp

    ws = materialize_workspace(task, tmp_path / "ws")

    scenario = scenarios.SCENARIOS[task.id]
    async with Client(mcp) as client:
        await scenario(client, ws)

    # Independent grading: fresh session, never trust the scenario.
    async with Client(mcp) as client:
        report = await grade(task, ws, client)
    assert report.passed, report.failures
