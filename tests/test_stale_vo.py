"""Tests for the stale-.vo dependency sweep (A10)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from rocq_mcp.compile import (
    _build_stale_dependencies_field,
    _scan_stale_vo,
    run_compile_file,
)
from tests.conftest import _fake_coqc_result


def _touch(path: Path, mtime: float) -> None:
    """Create *path* (with empty contents if new) and set its mtime."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("")
    os.utime(path, (mtime, mtime))


def _build_workspace(root: Path) -> dict[str, Path]:
    """Build a synthetic workspace with one fresh, one stale, one .vo-less .v.

    Also drops a stale .v/.vo pair inside ``_build/`` to verify pruning.
    Returns a name -> path map for assertions.
    """
    now = time.time()
    paths: dict[str, Path] = {}

    # Fresh pair: .vo newer than .v
    fresh_v = root / "fresh.v"
    fresh_vo = root / "fresh.vo"
    _touch(fresh_v, now - 100)
    _touch(fresh_vo, now - 10)
    paths["fresh_v"] = fresh_v

    # Stale pair: .v newer than .vo
    stale_v = root / "stale.v"
    stale_vo = root / "stale.vo"
    _touch(stale_vo, now - 100)
    _touch(stale_v, now - 10)
    paths["stale_v"] = stale_v

    # .v with no .vo (uncompiled — not flagged)
    uncompiled_v = root / "uncompiled.v"
    _touch(uncompiled_v, now - 10)
    paths["uncompiled_v"] = uncompiled_v

    # Stale pair under _build/ — must be pruned
    build_v = root / "_build" / "ignored.v"
    build_vo = root / "_build" / "ignored.vo"
    _touch(build_vo, now - 100)
    _touch(build_v, now - 10)
    paths["build_v"] = build_v

    # Stale pair under .hidden/ — must be pruned (hidden dirs skipped)
    hidden_v = root / ".hidden" / "ignored.v"
    hidden_vo = root / ".hidden" / "ignored.vo"
    _touch(hidden_vo, now - 100)
    _touch(hidden_v, now - 10)
    paths["hidden_v"] = hidden_v

    return paths


class TestScanStaleVo:
    """Direct exercises of ``_scan_stale_vo``."""

    def test_returns_only_stale(self, tmp_path):
        paths = _build_workspace(tmp_path)
        stale, truncated = _scan_stale_vo(tmp_path)
        assert truncated is False
        assert len(stale) == 1
        assert stale[0].resolve() == paths["stale_v"].resolve()

    def test_empty_workspace(self, tmp_path):
        stale, truncated = _scan_stale_vo(tmp_path)
        assert stale == []
        assert truncated is False

    def test_no_stale(self, tmp_path):
        now = time.time()
        _touch(tmp_path / "a.v", now - 100)
        _touch(tmp_path / "a.vo", now - 10)
        stale, truncated = _scan_stale_vo(tmp_path)
        assert stale == []
        assert truncated is False

    def test_truncation(self, tmp_path):
        now = time.time()
        for i in range(5):
            _touch(tmp_path / f"f{i}.vo", now - 100)
            _touch(tmp_path / f"f{i}.v", now - 10)
        stale, truncated = _scan_stale_vo(tmp_path, limit=3)
        assert truncated is True
        assert len(stale) == 3


class TestBuildStaleField:
    """Envelope-fragment builder behaviour."""

    def test_returns_none_when_clean(self, tmp_path):
        now = time.time()
        _touch(tmp_path / "a.v", now - 100)
        _touch(tmp_path / "a.vo", now - 10)
        assert _build_stale_dependencies_field(str(tmp_path)) is None

    def test_excludes_target_file(self, tmp_path):
        now = time.time()
        target = tmp_path / "target.v"
        _touch(tmp_path / "target.vo", now - 100)
        _touch(target, now - 10)
        # Only the target itself is "stale" — so the field should suppress.
        assert (
            _build_stale_dependencies_field(str(tmp_path), target_file=str(target))
            is None
        )

    def test_advisory_and_fields(self, tmp_path):
        _build_workspace(tmp_path)
        field = _build_stale_dependencies_field(str(tmp_path))
        assert field is not None
        assert field["count"] == 1
        assert field["truncated"] is False
        assert field["files"] == ["stale.v"]
        assert "newer than" in field["advisory"]
        assert "make" in field["advisory"]


class TestRunCompileFileEnvelope:
    """Integration: stale_dependencies field flows through ``run_compile_file``."""

    def test_field_present_on_success(self, tmp_path, monkeypatch):
        import rocq_mcp.compile as _compile

        # Build a workspace with a stale helper .v/.vo (separate from target).
        _build_workspace(tmp_path)
        target = tmp_path / "target.v"
        target.write_text("(* trivial *)\n")

        # Mock the actual coqc call so we don't need the binary.
        monkeypatch.setattr(
            _compile,
            "_run_coqc_file",
            lambda *a, **kw: {
                "returncode": 0,
                "stdout": "",
                "stderr": "",
                "timed_out": False,
            },
        )

        result = run_compile_file(
            file="target.v", workspace=str(tmp_path), timeout=30
        )
        assert result["success"] is True
        assert "stale_dependencies" in result
        assert result["stale_dependencies"]["count"] == 1
        assert "stale.v" in result["stale_dependencies"]["files"]

    def test_field_present_on_failure(self, tmp_path, monkeypatch):
        import rocq_mcp.compile as _compile

        _build_workspace(tmp_path)
        target = tmp_path / "target.v"
        target.write_text("Theorem bad : True.\n")

        monkeypatch.setattr(
            _compile,
            "_run_coqc_file",
            lambda *a, **kw: _fake_coqc_result(
                'File "target.v", line 1, characters 0-7:\n'
                "Error: Real failure.\n"
            ),
        )

        result = run_compile_file(
            file="target.v", workspace=str(tmp_path), timeout=30
        )
        assert result["success"] is False
        assert "stale_dependencies" in result
        assert result["stale_dependencies"]["count"] == 1

    def test_field_absent_when_clean(self, tmp_path, monkeypatch):
        import rocq_mcp.compile as _compile

        # Only the target file exists — no stale helpers.
        target = tmp_path / "target.v"
        target.write_text("(* trivial *)\n")

        monkeypatch.setattr(
            _compile,
            "_run_coqc_file",
            lambda *a, **kw: {
                "returncode": 0,
                "stdout": "",
                "stderr": "",
                "timed_out": False,
            },
        )

        result = run_compile_file(
            file="target.v", workspace=str(tmp_path), timeout=30
        )
        assert result["success"] is True
        assert "stale_dependencies" not in result
