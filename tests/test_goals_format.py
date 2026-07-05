"""goals_format rendering, goal diffs, and step_multi outcome dedup."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import rocq_mcp.pet_runtime as _pet_runtime
from rocq_mcp.interactive import _goals_diff, _render_goals
from tests.conftest import _MockPetBase


def _hyp(names, ty, def_=None):
    return SimpleNamespace(names=list(names), ty=ty, def_=def_)


def _goal(hyps, ty):
    return SimpleNamespace(hyps=hyps, ty=ty)


def _complete(goals, stack=(), shelf=(), given_up=()):
    return SimpleNamespace(
        goals=list(goals), stack=list(stack), shelf=list(shelf), given_up=list(given_up)
    )


G1 = _goal([_hyp(["n"], "nat"), _hyp(["H"], "n = 0")], "n + 0 = n")
G2 = _goal([_hyp(["m"], "nat", def_="0")], "m = m")


class TestRenderGoals:
    def test_pretty_is_the_classic_string(self):
        out = _render_goals([G1], "pretty")
        assert isinstance(out, str)
        assert "n : nat" in out and "|-n + 0 = n" in out

    def test_structured(self):
        out = _render_goals([G1, G2], "structured")
        assert out == [
            {
                "hyps": [
                    {"names": ["n"], "type": "nat"},
                    {"names": ["H"], "type": "n = 0"},
                ],
                "conclusion": "n + 0 = n",
            },
            {
                "hyps": [{"names": ["m"], "type": "nat", "body": "0"}],
                "conclusion": "m = m",
            },
        ]


class TestGoalsDiff:
    def test_added_and_removed(self):
        diff = _goals_diff([G1], [G1, G2])
        assert diff["before_count"] == 1
        assert diff["after_count"] == 2
        assert len(diff["added"]) == 1
        assert "m = m" in diff["added"][0]
        assert diff["removed_count"] == 0


class TestRunCheckGoalsFormat(_MockPetBase):
    def _run(self, goals_format, final_complete, parent_complete=None):
        import rocq_mcp.interactive as _interactive

        def fake_run(state, cmd, timeout=None):
            return SimpleNamespace(st=99, proof_finished=False, feedback=[])

        sid, mock_pet, lifespan_state = self._setup_state_and_pet(fake_run)
        completes = [final_complete]
        if parent_complete is not None:
            completes.append(parent_complete)
        mock_pet.complete_goals.side_effect = completes

        with patch.object(_pet_runtime, "_ensure_pet", return_value=mock_pet):
            return asyncio.run(
                _interactive.run_check(
                    body="intros.",
                    lifespan_state=lifespan_state,
                    from_state=sid,
                    goals_format=goals_format,
                )
            )

    def test_diff_mode(self):
        # Final state has G1+G2; parent had G1 -> one added goal.
        result = self._run("diff", _complete([G1, G2]), _complete([G1]))
        assert result["success"] is True
        assert "goals" not in result
        assert result["goals_diff"]["after_count"] == 2
        assert result["goals_diff"]["removed_count"] == 0
        assert "m = m" in result["goals_diff"]["added"][0]

    def test_invalid_format_is_a_validation_failure(self):
        import rocq_mcp.interactive as _interactive

        result = asyncio.run(
            _interactive.run_check(
                body="intros.",
                lifespan_state={"pet_timeout": 5.0},
                from_state=1,
                goals_format="bogus",
            )
        )
        assert result["success"] is False
        assert result["reason"] == "validation"
        assert "goals_format" in result["error"]


class TestStepMultiDedupBound(_MockPetBase):
    @pytest.mark.asyncio
    async def test_worst_case_payload_is_bounded(self):
        """20 tactics all reaching one identical (large) goal state must
        produce ONE goals payload, not twenty."""
        import rocq_mcp.interactive as _interactive

        def fake_run(state, cmd, timeout=None):
            return SimpleNamespace(st=7, proof_finished=False, feedback=[])

        sid, mock_pet, lifespan_state = self._setup_state_and_pet(fake_run)
        big_goal = _goal([_hyp(["h"], "X" * 6000)], "Y" * 1500)
        mock_pet.complete_goals.return_value = _complete([big_goal])

        tactics = [f"tac{i}." for i in range(20)]
        with patch.object(_pet_runtime, "_ensure_pet", return_value=mock_pet):
            result = await _interactive.run_step_multi(
                tactics=tactics,
                lifespan_state=lifespan_state,
                from_state=sid,
            )

        assert result["success"] is True
        assert result["distinct_outcomes"] == 1
        with_goals = [e for e in result["results"] if "goals" in e]
        assert len(with_goals) == 1
        refs = [e for e in result["results"] if e.get("same_outcome_as") == 0]
        assert len(refs) == 19
        payload = len(json.dumps(result))
        assert payload < 15_000, f"payload {payload} chars (pre-dedup: ~160KB)"
