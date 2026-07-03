"""Toolchain-free eval-harness invariants (run everywhere, unlike tier 1)."""

from __future__ import annotations

from evals.runner import scenarios
from evals.runner.common import load_corpus


def test_every_task_has_a_scenario():
    tasks = load_corpus()
    missing = [t.id for t in tasks if t.id not in scenarios.SCENARIOS]
    assert not missing, f"corpus tasks without a tier-1 scenario: {missing}"
    orphaned = [tid for tid in scenarios.SCENARIOS if tid not in {t.id for t in tasks}]
    assert not orphaned, f"scenarios without a corpus task: {orphaned}"


def test_tasks_are_well_formed():
    for task in load_corpus():
        assert task.kind in ("prove", "fix", "find_lemma"), task.id
        assert task.prompt.strip(), task.id
        assert task.check, f"{task.id} has no check block (ungradeable)"
        if task.kind in ("prove", "fix"):
            assert task.entry_file, task.id
            assert (task.path / "files" / task.entry_file).is_file(), task.id
