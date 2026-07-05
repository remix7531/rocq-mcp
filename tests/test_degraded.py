"""The degraded-enrichment convention (rocq_mcp.envelope).

Contract:
- ``degraded`` appears on a response only when at least one best-effort
  enrichment step failed; it is then a non-empty ``list[str]`` of stable
  ``"<field>:<code>"`` strings.
- It never appears as an empty list.
- ``degraded_detail`` appears only under ``ROCQ_DEBUG_ENRICHMENT=1``.
- Failures also bump ``lifespan_state["enrichment_failures"]`` counters
  (surfaced by rocq_diag).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from rocq_mcp import envelope
from tests.conftest import _MockPetBase


class TestPrimitives:
    def test_note_is_a_noop_outside_a_scope(self):
        # Must never raise or leak when no run_* scope is active.
        envelope.note_degraded("goals:pet_call_failed", "detail")

    async def test_decorator_attaches_only_when_noted(self):
        @envelope.collects_degraded
        async def clean_run(*, lifespan_state=None):
            return {"success": True}

        @envelope.collects_degraded
        async def degraded_run(*, lifespan_state=None):
            envelope.note_degraded("goals:pet_call_failed", "boom")
            return {"success": True}

        clean = await clean_run(lifespan_state=None)
        assert "degraded" not in clean

        got = await degraded_run(lifespan_state=None)
        assert got["degraded"] == ["goals:pet_call_failed"]
        assert "degraded_detail" not in got  # debug env not set


class TestEndToEnd(_MockPetBase):
    """Through the real run_check path with a mock pet whose goal fetch dies."""

    @pytest.mark.asyncio
    async def test_run_check_reports_degraded_goals(self):
        from rocq_mcp.interactive import run_check

        def fake_run(state, cmd, timeout=None):
            return SimpleNamespace(st=99, proof_finished=False, feedback=[])

        sid, mock_pet, lifespan_state = self._setup_state_and_pet(fake_run)
        lifespan_state["enrichment_failures"] = {}
        mock_pet.complete_goals.side_effect = RuntimeError("goal fetch died")

        result = await run_check(
            body="intros.",
            timeout=5.0,
            lifespan_state=lifespan_state,
            from_state=sid,
        )
        assert result["success"] is True
        assert result["goals"] == "(goals unavailable)"
        assert "goals:pet_call_failed" in result["degraded"]
        assert lifespan_state["enrichment_failures"]["goals:pet_call_failed"] >= 1
