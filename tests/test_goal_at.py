"""Tests for run_goal_at() — stateless, file-anchored goal inspection.

run_goal_at() returns goals at a (file, line, character) position without
ever inserting an entry into ``_state_table``.  These tests exercise that
contract: the state table size MUST NOT grow as a side-effect.
"""

from __future__ import annotations

import pytest

from tests.conftest import PET_AVAILABLE, make_lifespan_state

pytestmark = pytest.mark.skipif(not PET_AVAILABLE, reason="pet not available")


@pytest.fixture(autouse=True)
def reset_state_table():
    from rocq_mcp.interactive import _state_invalidate_all

    _state_invalidate_all()
    yield
    _state_invalidate_all()


@pytest.fixture
def lifespan_state():
    from rocq_mcp.server import _invalidate_pet

    state = make_lifespan_state()
    yield state
    _invalidate_pet(state)


@pytest.fixture
def proof_vfile(workspace):
    """A .v file with a multi-line proof so we can probe mid-proof."""
    vfile = workspace / "goal_at_test.v"
    vfile.write_text(
        "Theorem my_thm : forall n : nat, n = n.\n"
        "Proof.\n"
        "  intros.\n"
        "  reflexivity.\n"
        "Qed.\n"
    )
    return str(vfile)


class TestGoalAtSuccess:
    @pytest.mark.asyncio
    async def test_returns_goals_in_proof(
        self, workspace, lifespan_state, proof_vfile
    ):
        """A position inside an open proof returns formatted goals."""
        from rocq_mcp.interactive import run_goal_at

        # Position right after `Proof.` — one goal still open.
        result = await run_goal_at(
            file=proof_vfile,
            line=2,
            character=0,
            workspace=str(workspace),
            lifespan_state=lifespan_state,
        )
        assert result["success"] is True
        assert result["stateless"] is True
        assert result["at_proof"] is True
        assert "n = n" in result["goals"]

    @pytest.mark.asyncio
    async def test_does_not_register_state(
        self, workspace, lifespan_state, proof_vfile
    ):
        """run_goal_at MUST NOT add an entry to ``_state_table``."""
        from rocq_mcp.interactive import (
            _state_next_id,
            _state_table,
            run_goal_at,
        )
        import rocq_mcp.interactive as _interactive

        before_size = len(_state_table)
        before_next_id = _interactive._state_next_id

        result = await run_goal_at(
            file=proof_vfile,
            line=2,
            character=0,
            workspace=str(workspace),
            lifespan_state=lifespan_state,
        )
        assert result["success"] is True

        # Re-read module-level after call.
        assert len(_state_table) == before_size, (
            "state_table grew; rocq_goal_at must be stateless"
        )
        assert _interactive._state_next_id == before_next_id, (
            "state_next_id incremented; rocq_goal_at must be stateless"
        )

    @pytest.mark.asyncio
    async def test_position_outside_proof(
        self, workspace, lifespan_state, proof_vfile
    ):
        """A position in vernac (before the theorem) returns at_proof=False."""
        from rocq_mcp.interactive import run_goal_at

        result = await run_goal_at(
            file=proof_vfile,
            line=0,
            character=0,
            workspace=str(workspace),
            lifespan_state=lifespan_state,
        )
        assert result["success"] is True
        assert result["stateless"] is True
        assert result["at_proof"] is False
        assert result["goals"] == ""


class TestGoalAtErrors:
    @pytest.mark.asyncio
    async def test_file_not_found(self, workspace, lifespan_state):
        """Missing file returns a validation failure envelope."""
        from rocq_mcp.interactive import run_goal_at

        result = await run_goal_at(
            file="nope_does_not_exist.v",
            line=0,
            character=0,
            workspace=str(workspace),
            lifespan_state=lifespan_state,
        )
        assert result["success"] is False
        assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_negative_line_rejected(self, workspace, lifespan_state):
        """Out-of-range line returns a validation failure."""
        from rocq_mcp.interactive import run_goal_at

        result = await run_goal_at(
            file="anything.v",
            line=-1,
            character=0,
            workspace=str(workspace),
            lifespan_state=lifespan_state,
        )
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_timeout_is_plumbed(
        self, workspace, lifespan_state, proof_vfile, monkeypatch
    ):
        """The ``timeout`` arg is forwarded to ``_run_with_pet``."""
        import rocq_mcp.server as _server
        from rocq_mcp.interactive import run_goal_at

        captured: dict = {}
        real_run_with_pet = _server._run_with_pet

        async def _spy(*args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            return await real_run_with_pet(*args, **kwargs)

        monkeypatch.setattr(_server, "_run_with_pet", _spy)

        await run_goal_at(
            file=proof_vfile,
            line=2,
            character=0,
            workspace=str(workspace),
            lifespan_state=lifespan_state,
            timeout=7.5,
        )
        assert captured["timeout"] == 7.5
