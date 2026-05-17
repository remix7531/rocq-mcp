"""Tests for run_hover() — stateless, file-anchored symbol info.

run_hover() synthesises hover output from pytanque's ``ast_at_pos`` +
``get_state_at_pos`` + ``Locate``/``About`` because petanque does not
expose an LSP ``textDocument/hover`` route.  These tests verify the
contract (statelessness, no-symbol sentinel, timeout plumbing) and the
happy-path field extraction for a stdlib symbol.
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
def nat_vfile(workspace):
    """A .v file mentioning ``nat`` so hover has something to resolve."""
    vfile = workspace / "hover_test.v"
    vfile.write_text(
        "Definition d : nat := 0.\n"
        "\n"
    )
    return str(vfile)


class TestHoverHappy:
    @pytest.mark.asyncio
    async def test_hover_on_nat_returns_name(
        self, workspace, lifespan_state, nat_vfile
    ):
        """Hover on ``nat`` resolves the identifier and runs Locate/About."""
        from rocq_mcp.interactive import run_hover

        # "Definition d : nat := 0." — ``nat`` starts at character 15.
        result = await run_hover(
            file=nat_vfile,
            line=0,
            character=15,
            workspace=str(workspace),
            lifespan_state=lifespan_state,
        )
        assert result["success"] is True
        assert result["stateless"] is True
        assert result["found"] is True
        assert result["name"] == "nat"
        # At least one of raw_about / raw_locate should be populated.
        assert (result.get("raw_about") or result.get("raw_locate")) is not None

    @pytest.mark.asyncio
    async def test_hover_does_not_register_state(
        self, workspace, lifespan_state, nat_vfile
    ):
        """run_hover MUST NOT add an entry to ``_state_table``."""
        from rocq_mcp.interactive import _state_table, run_hover
        import rocq_mcp.interactive as _interactive

        before_size = len(_state_table)
        before_next_id = _interactive._state_next_id

        result = await run_hover(
            file=nat_vfile,
            line=0,
            character=15,
            workspace=str(workspace),
            lifespan_state=lifespan_state,
        )
        assert result["success"] is True
        assert len(_state_table) == before_size
        assert _interactive._state_next_id == before_next_id


class TestHoverSentinels:
    @pytest.mark.asyncio
    async def test_hover_on_whitespace(
        self, workspace, lifespan_state, nat_vfile
    ):
        """Whitespace position returns found=False with a reason marker."""
        from rocq_mcp.interactive import run_hover

        # Line 1 (the blank line) — no identifier there.
        result = await run_hover(
            file=nat_vfile,
            line=1,
            character=0,
            workspace=str(workspace),
            lifespan_state=lifespan_state,
        )
        assert result["success"] is True
        assert result["stateless"] is True
        assert result["found"] is False
        assert result.get("reason") == "no_symbol_at_position"


class TestHoverErrors:
    @pytest.mark.asyncio
    async def test_file_not_found(self, workspace, lifespan_state):
        from rocq_mcp.interactive import run_hover

        result = await run_hover(
            file="nope_does_not_exist.v",
            line=0,
            character=0,
            workspace=str(workspace),
            lifespan_state=lifespan_state,
        )
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_timeout_is_plumbed(
        self, workspace, lifespan_state, nat_vfile, monkeypatch
    ):
        import rocq_mcp.server as _server
        from rocq_mcp.interactive import run_hover

        captured: dict = {}
        real_run_with_pet = _server._run_with_pet

        async def _spy(*args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            return await real_run_with_pet(*args, **kwargs)

        monkeypatch.setattr(_server, "_run_with_pet", _spy)

        await run_hover(
            file=nat_vfile,
            line=0,
            character=15,
            workspace=str(workspace),
            lifespan_state=lifespan_state,
            timeout=5.0,
        )
        assert captured["timeout"] == 5.0
