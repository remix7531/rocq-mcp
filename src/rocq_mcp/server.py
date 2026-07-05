"""Rocq MCP Server — tools for Rocq/Coq proof development.

This is the main entry point.  It defines the MCP application, shared
infrastructure (configuration, workspace validation, pet subprocess
management), and thin ``@mcp.tool`` wrappers that delegate to
implementation functions in :mod:`rocq_mcp.compile` and
:mod:`rocq_mcp.interactive`.
"""

from __future__ import annotations

import asyncio
import collections
import os
import shutil
import warnings
from pathlib import Path
from typing import Any

from fastmcp import FastMCP, Context
from fastmcp.server.lifespan import lifespan
from mcp.types import ToolAnnotations

import rocq_mcp
from rocq_mcp import config
from rocq_mcp import pet_runtime as _pet_runtime
from rocq_mcp import workspace as _workspace

# Implementation functions from the domain modules (the old circular
# import is gone — these are ordinary top-level imports now).
from rocq_mcp.compile import (
    run_verify,
)
from rocq_mcp.compile_enrichment import (
    run_compile_file_with_state,
    run_compile_with_state,
)

# ---------------------------------------------------------------------------
# Configuration (env vars with defaults)
# ---------------------------------------------------------------------------
# Env-derived configuration lives in rocq_mcp.config (single definition
# site).  The names are re-bound here because submodules read them as
# ``_server.<NAME>`` and tests monkeypatch them on this module — both
# work against these bindings, since server code reads its own globals.
# These are unused *within* server.py but read cross-module as
# ``_server.<NAME>`` (interactive / compile_enrichment / tests) — the
# per-name noqa keeps unused-import autofixes from severing those
# attribute paths (which is exactly what broke 32 tests when it
# happened once).
from rocq_mcp.config import (
    _COMPILE_MULTI_ERROR_CAP,  # noqa: F401
    _COMPILE_MULTI_ERROR_TIMEOUT,  # noqa: F401
    _RECENT_ERRORS_MAX,
    ROCQ_MAX_SOURCE_SIZE,  # noqa: F401
    ROCQ_WORKSPACE,  # noqa: F401  (import-compat; internal reads use config.ROCQ_WORKSPACE)
    _default_max_pet_rss_mb,  # noqa: F401
)
from rocq_mcp.diag import (
    _DIAG_LIVE_STATES_CAP,  # noqa: F401  (accessed as _server._DIAG_LIVE_STATES_CAP in tests)
    _build_diag_snapshot,
    _sample_pet_rss_mb,  # noqa: F401  (accessed as _server._sample_pet_rss_mb in tests)
)

# The failure-reason taxonomy and the envelope/degradation helpers moved
# to leaf modules (single source of truth; no import cycle).  The names
# below stay bound on this module because submodules and tests access
# them as ``_server.<name>`` — see rocq_mcp/taxonomy.py and
# rocq_mcp/envelope.py for the definitions.
from rocq_mcp.envelope import (
    _RECENT_ERROR_MESSAGE_LIMIT,  # noqa: F401  (re-export for tests)
    _fail,
    _no_ctx_fail,
    _record_error,
)
from rocq_mcp.health import (
    _SWITCH_ENV_KEYS,
    _detect_switch,
    _resolve_binary,
    build_health_snapshot,
    compute_switch_env,
    list_switches,
)
from rocq_mcp.interactive import (
    run_assumptions,
    run_check,
    run_notations,
    run_query,
    run_start,
    run_step_multi,
    run_toc,
)

# Façade re-exports for import-and-call compatibility (tests do
# ``from rocq_mcp.server import _kill_pet`` etc.).  The two REBOUND
# mutable globals (_pet_lock, _pet_semaphore) are deliberately NOT
# re-exported — a from-import copy goes stale the moment
# _force_release_pet_lock replaces the lock; access them as
# ``pet_runtime._pet_lock`` / ``pet_runtime._pet_semaphore`` only.
from rocq_mcp.pet_runtime import (  # noqa: F401
    _PYTANQUE_NOT_INSTALLED_HINT,
    _build_memory_abort_response,
    _ensure_pet,
    _force_release_pet_lock,
    _get_pet_semaphore,
    _handle_pet_failure,
    _invalidate_pet,
    _kill_pet,
    _memory_watchdog,
    _merge_partial_state,
    _pet_alive,
    _pet_invalidation_hooks,
    _PetLockTimeout,
    _run_with_pet,
    _set_workspace_if_needed,
    _try_close_pet,
)
from rocq_mcp.taxonomy import (
    RECENT_ERROR_REASONS as _RECENT_ERROR_REASONS,  # noqa: F401
)

# Façade re-exports: tests import-and-call these via rocq_mcp.server, and
# submodules still read some as ``_server.<NAME>``.  Server-internal code
# never uses these bindings (it calls ``_workspace.<NAME>``), so a
# monkeypatch on rocq_mcp.workspace is authoritative for every code path.
from rocq_mcp.workspace import (  # noqa: F401
    _CLEANUP_EXTENSIONS,
    _PROJECT_MARKERS,
    _VO_SCAN_FILE_CAP,
    _cleanup_coqc_artifacts,
    _count_sessions_in_workspace,
    _diff_vo_mtimes,
    _find_dune_root,
    _find_project_root_from_file,
    _maybe_vo_rebuild_warning,
    _maybe_workspace_warning,
    _parse_dune_flags,
    _parse_project_flags,
    _path_within,
    _pick_v_file,
    _resolve_file_in_workspace,
    _snapshot_vo_mtimes,
    _validate_workspace,
    _workspace_has_project_marker,
)


def _check_timeout_config(pet_timeout: float, cap: int) -> str | None:
    """Return a warning if config.ROCQ_PET_TIMEOUT exceeds config.ROCQ_QUERY_TIMEOUT_CAP.

    The cap is documented in the README as the upper bound for the
    per-call timeout, but config.ROCQ_PET_TIMEOUT is the fallback when no
    per-call timeout is given.  If an operator misconfigures the pair
    so the fallback exceeds the cap, the lock can park longer than the
    cap promise — silently violating the documented invariant.
    """
    if pet_timeout > cap:
        return (
            f"ROCQ_PET_TIMEOUT={pet_timeout} exceeds config.ROCQ_QUERY_TIMEOUT_CAP={cap}; "
            f"calls without a per-call timeout= will park the pet lock longer "
            f"than config.ROCQ_QUERY_TIMEOUT_CAP claims."
        )
    return None


_timeout_config_msg = _check_timeout_config(
    config.ROCQ_PET_TIMEOUT, config.ROCQ_QUERY_TIMEOUT_CAP
)
if _timeout_config_msg:
    warnings.warn(_timeout_config_msg, RuntimeWarning, stacklevel=2)


# _PYTANQUE_NOT_INSTALLED_HINT moved to rocq_mcp.pet_runtime.


def _check_pet_availability() -> str | None:
    """Return a warning message when pet (pytanque + ``pet`` binary) is missing.

    The interactive tools and the multi-error / state-capture enrichment on
    ``rocq_compile_file`` all route through pet.  Falling back to coqc-only
    operation is a substantial reduction in capability, so the operator
    deserves an up-front signal at server boot rather than discovering it
    only when an agent dispatches the first interactive tool call and gets
    ``reason="unavailable"`` back.

    Returns ``None`` when both halves of the install are present.
    """
    pytanque_missing = False
    try:
        import pytanque  # noqa: F401
    except ImportError:
        pytanque_missing = True
    pet_binary_missing = shutil.which("pet") is None
    if not (pytanque_missing or pet_binary_missing):
        return None
    parts = []
    if pytanque_missing:
        parts.append("the pytanque Python binding is not importable")
    if pet_binary_missing:
        parts.append("the `pet` binary is not on PATH")
    return (
        "pet not detected: " + " and ".join(parts) + ". "
        "Interactive tools (rocq_start / rocq_check / rocq_step_multi / "
        "rocq_query / rocq_assumptions / rocq_toc / rocq_notations) and "
        "proof-state enrichment on rocq_compile_file will return "
        'reason="unavailable". '
        "Petanque (the `pet` binary and the matching pytanque Python "
        "binding) ships with coq-lsp; both halves must be installed "
        "together.  `pip` / `uv` cannot install petanque on their own — "
        "see https://github.com/ejgallego/coq-lsp for install "
        "instructions appropriate to your environment."
    )


_pet_availability_msg = _check_pet_availability()
if _pet_availability_msg:
    warnings.warn(_pet_availability_msg, RuntimeWarning, stacklevel=2)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@lifespan
async def app_lifespan(server: Any) -> Any:
    """Server lifespan. Pet is spawned lazily on first pytanque call."""
    state: dict[str, Any] = {
        "pet_client": None,
        "workspace": config.ROCQ_WORKSPACE,
        "pet_timeout": config.ROCQ_PET_TIMEOUT,
        "current_workspace": None,
        # Diagnostics (rocq_diag tool, see _build_diag_snapshot).
        "pet_started_at": None,
        # Count of successful spawns; pet_restarts is derived as
        # max(0, total_spawns - 1).  Counting only successful spawns
        # ensures a fresh server reports 0 restarts even if the very
        # first spawn attempt raised.
        "total_spawns": 0,
        "peak_pet_rss_mb": 0.0,
        "pet_generation": 0,
        "recent_errors": collections.deque(maxlen=_RECENT_ERRORS_MAX),
        # Degraded-enrichment counters (rocq_mcp.envelope.attach_degraded)
        # and pet-lock contention telemetry — surfaced by rocq_diag.
        "enrichment_failures": {},
        "lock_wait_ms_last": 0.0,
        "lock_wait_ms_max": 0.0,
        "lock_contended_total": 0,
    }
    try:
        yield state
    finally:
        client = state.get("pet_client")
        if client:
            _pet_runtime._kill_pet(client)
        # Clean up cache file
        ws = state.get("workspace")
        if ws:
            cache_file = Path(ws) / f"rocq_mcp_cache_{os.getpid()}_.v"
            _workspace._cleanup_coqc_artifacts(str(cache_file))


# Always-visible server guidance.  With deferred tool loading (the default
# in Claude Code) tool descriptions may not be in context until a tool is
# looked up — these instructions are the one place cross-tool knowledge is
# guaranteed to be visible.  Budget: keep under ~2,200 characters.
_SERVER_INSTRUCTIONS = """\
Rocq/Coq proof tools: coqc-based batch compile/verify plus a held \
interactive session (pet/coq-lsp) that keeps imports warm across calls.

Core proof loop: rocq_start (returns a state_id) -> rocq_step_multi to \
explore candidate tactics (read-only) -> rocq_check to commit the winner \
(returns a new state_id) -> write the finished proof into the .v file -> \
rocq_compile_file + rocq_verify (and rocq_assumptions for an axiom audit) \
to finish. Never iterate by re-running coqc on a scratch file: coqc \
reloads every import per call; the interactive session does not.

State rules: rocq_check and rocq_step_multi require an explicit \
from_state (a state_id from rocq_start or a previous rocq_check). States \
live in a process-global LRU table; states you keep using stay alive. If \
a state_id goes missing, restart the session with rocq_start.

Failures: every failure response is {success: false, error, reason}. \
Dispatch on reason, not on message text; reason is one of: validation, \
not_found, timeout, crashed, memory_exhausted, lock_contended, \
unavailable, tactic_failed, compile_error, axiom_dependency, \
type_mismatch. When a response carries pet_restarted: true, call \
rocq_diag to see what happened.

Timeouts: timeout=0 means "use the server default". On session/query \
tools larger per-call values are clamped to a server cap and the \
response then carries clamped_timeout; compile/verify timeouts are \
used as-is.
"""

mcp = FastMCP(
    "rocq-mcp",
    instructions=_SERVER_INSTRUCTIONS,
    version=rocq_mcp.__version__,
    lifespan=app_lifespan,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Workspace resolution / project flags / artifact hygiene -> rocq_mcp.workspace
# ---------------------------------------------------------------------------
# Moved verbatim to rocq_mcp/workspace.py.  Names are re-bound below
# because tests and submodules read them as ``_server.<NAME>``; internal
# server code calls through ``_workspace.<NAME>`` (attribute access) so a
# monkeypatch on rocq_mcp.workspace is seen by every caller.


def _resolve_call_timeout(
    timeout: int | float | None,
) -> tuple[float | None, bool]:
    """Resolve a per-call timeout against ``config.ROCQ_QUERY_TIMEOUT_CAP``.

    Returns ``(effective_timeout, clamped)``:

    - When *timeout* is ``None``, 0, or negative: ``(None, False)`` —
      caller falls back to ``config.ROCQ_PET_TIMEOUT``.
    - Otherwise: ``(float(min(timeout, cap)), timeout > cap)``.

    Shared by every pet-routed wrapper so the clamp behaviour and
    ``clamped_timeout`` echo stay in lockstep.
    """
    if timeout is None or timeout <= 0:
        return None, False
    clamped = timeout > config.ROCQ_QUERY_TIMEOUT_CAP
    return float(min(timeout, config.ROCQ_QUERY_TIMEOUT_CAP)), clamped


# ---------------------------------------------------------------------------
# Pet subprocess management -> rocq_mcp.pet_runtime (decomposition cluster B)
# ---------------------------------------------------------------------------


def _resolve_tool_envelope(
    *,
    tool: str,
    ctx: Any,
    workspace: str,
    file: str | None = None,
    timeout: int | float | None = None,
    timeout_default: int | None = None,
    ctx_optional: bool = False,
) -> dict[str, Any] | tuple[str, dict[str, Any] | None, str | None, bool, float | None]:
    """Resolve the shared envelope steps for ``@mcp.tool`` wrappers.

    Runs the boilerplate every pet-routed / coqc-routed wrapper repeats:

    1. ctx-check first — for pet-routed tools, a missing context is a
       programmer error and is reported as the no-ctx envelope without
       touching the workspace (``_no_ctx_fail``).  Pins the order so a
       bad workspace + no-ctx caller gets the no-ctx envelope, not a
       silent validation failure that ``recent_errors`` never sees.
       Coqc-routed tools (``rocq_compile`` / ``rocq_compile_file`` /
       ``rocq_verify``) pass ``ctx_optional=True``: they can run without
       an MCP context — ``lifespan_state`` falls through as ``None`` and
       the ``recent_errors`` recording is silently skipped.
    2. Workspace resolution: explicit > project-marker walk-up from
       *file* (when *file* is non-empty) > ``config.ROCQ_WORKSPACE``.
    3. Timeout resolution.  *timeout_default* is the wrapper's
       compile/verify fallback (seconds): when set, the helper
       falls back to it for ``timeout<=0`` and does not clamp; when
       ``None`` the helper routes through :func:`_resolve_call_timeout`
       (the per-call cap that returns a ``clamped`` flag).
    4. ``_workspace._validate_workspace`` against the resolved workspace; on
       failure returns a :func:`_fail` envelope with the *already
       resolved* lifespan_state so the failure lands in
       ``recent_errors``.
    5. ``_workspace._maybe_workspace_warning`` against the resolved workspace.

    Returns either a failure envelope (caller returns it verbatim) or
    the tuple ``(workspace, lifespan_state, ws_warning, clamped,
    effective_timeout)``.
    """
    if ctx is None:
        if not ctx_optional:
            return _no_ctx_fail(tool)
        lifespan_state: dict[str, Any] | None = None
    else:
        lifespan_state = ctx.lifespan_context

    explicit_workspace = bool(workspace)
    if file:
        workspace = (
            workspace
            or _workspace._find_project_root_from_file(file)
            or config.ROCQ_WORKSPACE
        )
    else:
        workspace = workspace or config.ROCQ_WORKSPACE

    effective_timeout: float | None
    if timeout_default is not None:
        effective_timeout = (
            float(timeout)
            if timeout is not None and timeout > 0
            else float(timeout_default)
        )
        clamped = False
    else:
        effective_timeout, clamped = _resolve_call_timeout(timeout)

    err = _workspace._validate_workspace(workspace)
    if err:
        return _fail(lifespan_state, tool, err, "validation")

    ws_warning = _workspace._maybe_workspace_warning(
        workspace, explicit=explicit_workspace, file_provided=bool(file)
    )
    return workspace, lifespan_state, ws_warning, clamped, effective_timeout


def _finalize_tool_envelope(
    result: Any, *, clamped: bool, ws_warning: str | None
) -> Any:
    """Merge trailing envelope keys onto *result*.

    Mirrors the trailing 3-line block of every wrapper:

    - ``clamped_timeout``: echoes the cap value when the per-call
      timeout was clamped by :func:`_resolve_call_timeout`.
    - ``workspace_warning``: the advisory from
      :func:`_workspace._maybe_workspace_warning`, when set.

    Both merges are no-ops if *result* is not a ``dict`` so an
    implementation that returns a non-dict (unexpected) passes through
    untouched.
    """
    if not isinstance(result, dict):
        return result
    if clamped:
        result["clamped_timeout"] = config.ROCQ_QUERY_TIMEOUT_CAP
    if ws_warning:
        result["workspace_warning"] = ws_warning
    return result


# ---------------------------------------------------------------------------
# Tool: rocq_compile
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        title="Compile Rocq source (coqc)",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def rocq_compile(
    source: str,
    workspace: str = "",
    timeout: int = 0,
    include_warnings: bool = True,
    ctx: Context = None,
) -> dict[str, Any]:
    """Compile a finished .v file via coqc.

    For *scratch iteration* on a single proof, prefer the interactive tools:

    - ``rocq_start`` opens a held session (imports stay warm across
      attempts).
    - ``rocq_check`` runs a candidate proof body against a held state.
    - ``rocq_step_multi`` tries several tactics at once against a held
      state and reports which succeeded.

    Use ``rocq_compile`` for finished proofs, axiom audits, and final
    verification — coqc reloads all imports per call (often several
    seconds on heavy library imports).

    On failure, the result includes ``error_positions`` and a ``hint``.
    When coq-lsp is available in the active MCP session, the result
    also includes ``state_capture_status``:

      - ``"ok"``: proof state was captured at the error position; the
        result also includes ``state_id``, ``goals``, ``file``,
        ``theorem``, and ``proof_finished``.  Recover via
        ``rocq_check(from_state=state_id)`` or
        ``rocq_step_multi(from_state=state_id)``.
      - ``"outside_proof"``: error is outside any open proof; no
        ``state_id`` is returned.  Follow the original ``hint``.
      - ``"timeout"`` / ``"crashed"`` / ``"lock_contended"`` /
        ``"unavailable"`` / ``"memory_exhausted"`` /
        ``"no_position"``: enrichment did not
        succeed; follow the original ``hint`` (typically
        ``rocq_start(file=..., line=..., character=...)``).

    Args:
        source: Complete Rocq (.v) file content to compile.
        workspace: Directory to use as workspace (default: ROCQ_WORKSPACE env var).
        timeout: Compilation timeout in seconds (default: ROCQ_COQC_TIMEOUT env var).
        include_warnings: If True (default), include deduplicated warnings
            before the error in the output.  Set to False to get only the
            error diagnostic, which keeps context compact.

    On ``pet_restarted: True`` (state-capture path crashed pet), call
    ``rocq_diag`` for memory headroom and recent error history.
    """
    resolved = _resolve_tool_envelope(
        tool="rocq_compile",
        ctx=ctx,
        workspace=workspace,
        timeout=timeout,
        timeout_default=config.ROCQ_COQC_TIMEOUT,
        ctx_optional=True,
    )
    if not isinstance(resolved, tuple):
        return resolved
    workspace, lifespan_state, ws_warning, clamped, effective_timeout = resolved

    # timeout_default was passed to _resolve_tool_envelope,
    # so effective_timeout is non-None here.
    assert effective_timeout is not None
    result = await run_compile_with_state(
        source=source,
        workspace=workspace,
        timeout=effective_timeout,
        include_warnings=include_warnings,
        lifespan_state=lifespan_state,
    )
    return _finalize_tool_envelope(result, clamped=clamped, ws_warning=ws_warning)


# ---------------------------------------------------------------------------
# Tool: rocq_compile_file
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        title="Compile a .v file (coqc)",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def rocq_compile_file(
    file: str,
    workspace: str = "",
    timeout: int = 0,
    include_warnings: bool = True,
    keep_vo: bool = False,
    mode: str = "full",
    timing: bool = False,
    ctx: Context = None,
) -> dict[str, Any]:
    """Compile a finished .v file on disk via coqc.

    For *scratch iteration* on a single proof, prefer the interactive tools:

    - ``rocq_start`` opens a held session (imports stay warm across
      attempts).
    - ``rocq_check`` runs a candidate proof body against a held state.
    - ``rocq_step_multi`` tries several tactics at once against a held
      state and reports which succeeded.

    Use ``rocq_compile_file`` for whole-file verification, axiom audits,
    and final compile — coqc reloads all imports per call (often several
    seconds on heavy library imports).  Preferred over ``rocq_compile``
    for large files because the source stays on disk (avoids transmitting
    the full text through the MCP transport).

    On failure, the result includes ``error_positions`` and a ``hint``.
    When coq-lsp is available in the active MCP session, the result
    also includes ``state_capture_status``:

      - ``"ok"``: proof state was captured at the error position; the
        result also includes ``state_id``, ``goals``, ``file``,
        ``theorem``, and ``proof_finished``.  Recover via
        ``rocq_check(from_state=state_id)`` or
        ``rocq_step_multi(from_state=state_id)``.
      - ``"outside_proof"``: error is outside any open proof; no
        ``state_id`` is returned.  Follow the original ``hint``.
      - ``"timeout"`` / ``"crashed"`` / ``"lock_contended"`` /
        ``"unavailable"`` / ``"memory_exhausted"`` /
        ``"no_position"``: enrichment did not
        succeed; follow the original ``hint`` (typically
        ``rocq_start(file=..., line=..., character=...)``).

    On a ``compile_error`` failure with coq-lsp available, the result
    may also include ``errors``: a list of per-proof errors discovered
    by walking the file through pet (one entry per failing chunk).
    Each entry is ``{proof_name, kind, start_line, end_line, code,
    message}``.  Complements ``error_positions`` — the latter is
    coqc's raw parse of the first diagnostic, while ``errors`` is
    pet's structured walk of the whole file.  The field may be
    *present and empty* (``errors: []``) when the walker ran but pet
    did not reproduce the coqc-reported failure — treat this as "no
    additional errors found" rather than "no errors at all."  Absent
    on success, when coq-lsp is unavailable, when the walker could
    not run, and when ``ROCQ_COMPILE_MULTI_ERROR_CAP=0`` (feature
    disabled).  Tune via ``ROCQ_COMPILE_MULTI_ERROR_CAP`` (default 20,
    max entries) and ``ROCQ_COMPILE_MULTI_ERROR_TIMEOUT`` (default
    5.0s, per-``pet.run`` budget inside the walker).

    Compilation artifacts (``.vo``/``.vok``/``.vos``/``.glob``/``.aux``)
    are cleaned up by default; the source file is preserved.  Set
    ``keep_vo=True`` to retain the compiled-artifact family
    (``.vo``/``.vok``/``.vos``) while still cleaning the diagnostic
    artifacts (``.glob``/``.aux``/``.vio``/``.timing``/``.coqaux``).
    Typical use: compiling a file whose ``.vo`` will be imported by a
    sibling ``.v`` in the same workspace, or incremental compile loops
    that want to avoid rebuilding unchanged dependencies.

    When the call rewrites ``.vo`` files in a workspace that has active
    interactive sessions, the result also includes ``vo_rebuild_warning``:
    a soft advisory naming the workspace and the count of potentially
    affected sessions, with a hint to call ``rocq_start`` again to refresh
    held dependency state.  Quiet when no ``.vo`` changed, when no
    interactive session in this workspace exists, or when the workspace
    exceeds ``_VO_SCAN_FILE_CAP`` (.vo paths).  Setting ``keep_vo=True``
    makes this warning *more likely to fire* on subsequent
    ``rocq_compile_file`` calls in the same workspace: the produced
    ``.vo`` now persists between calls, so any later compile that
    rewrites it is observable as a fresh mtime delta.

    Args:
        file: Path to the .v file (relative to workspace).
        workspace: Workspace directory.  If omitted, auto-detected by walking
            up from *file* looking for ``_RocqProject`` / ``_CoqProject`` /
            ``dune-project``; falls back to the ``ROCQ_WORKSPACE`` env var
            (default: cwd).
        timeout: Compilation timeout in seconds (default: ROCQ_COQC_TIMEOUT env var).
        include_warnings: If True (default), include deduplicated warnings
            before the error in the output.  Set to False to get only the
            error diagnostic, which keeps context compact.
        keep_vo: If True, preserve the ``.vo``/``.vok``/``.vos`` outputs
            after coqc returns (diagnostic artifacts are still cleaned).
            Default False matches today's "clean everything but the
            source" behavior.  Useful when a sibling file in the same
            workspace will ``Require Import`` the result.  **Note**:
            combining ``keep_vo=True`` with ``mode="vos"`` produces
            only a ``.vos`` artifact; downstream files compiled in
            ``mode="full"`` will fail with ``"Unable to locate
            library ... (while searching for a .vos file)"`` — use
            ``mode="full" keep_vo=True`` when the sibling consumer
            expects a ``.vo``.
        mode: Which coqc pass to run.  ``"full"`` (default) is today's
            behavior — coqc fully elaborates every proof body.  ``"vos"``
            adds ``-vos`` so coqc *skips proof bodies entirely* — it
            does NOT execute them.  ``"vos"`` is fast and catches
            missing imports, statement type errors, holes left in
            statements, and notation conflicts.  It does NOT validate
            proofs: a ``Theorem t : False. Proof. exact I. Qed.``
            passes under ``"vos"``.  Use it as a cheap pre-pass during
            iteration, then run ``"full"`` for the real check.
            ``"vos"`` produces a ``.vos`` artifact rather than a ``.vo``.
        timing: If True, invoke coqc with ``-time`` and attach a
            ``timing`` field to the response with per-sentence
            diagnostics — ``{"total_sentences": int, "top_slowest":
            list[{line, characters, name, duration_seconds}],
            "last_completed": {...} | None}``.  ``top_slowest`` holds
            up to 5 entries sorted by descending duration.  On
            timeout, ``last_completed`` is the final sentence coqc
            finished and the ``error`` string names it so "timed out
            after 590s" becomes "Last completed sentence: line 221
            [Theorem.foo] (15.3s)."  On a successful compile,
            ``last_completed`` is the file's literal final sentence
            (not a failure marker).  Default False is zero-overhead.

    The response envelope additionally carries several optional fields
    depending on flags / failure mode: ``error_positions`` and
    ``state_capture_status`` on ``reason="compile_error"`` (see the
    ``state_capture_status`` paragraph above); ``errors`` per-declaration
    list when ``pet`` is available (see the Multi-error callout in the
    README); ``vo_rebuild_warning`` when the call rewrites ``.vo``
    artifacts in a workspace with active sessions; ``clamped_timeout``
    when the per-call timeout was clamped by ``ROCQ_QUERY_TIMEOUT_CAP``;
    ``timing`` when ``timing=True``.

    On ``pet_restarted: True`` (state-capture path crashed pet), call
    ``rocq_diag`` for memory headroom and recent error history.
    """
    resolved = _resolve_tool_envelope(
        tool="rocq_compile_file",
        ctx=ctx,
        workspace=workspace,
        file=file,
        timeout=timeout,
        timeout_default=config.ROCQ_COQC_TIMEOUT,
        ctx_optional=True,
    )
    if not isinstance(resolved, tuple):
        return resolved
    workspace, lifespan_state, ws_warning, clamped, effective_timeout = resolved

    # timeout_default was passed to _resolve_tool_envelope,
    # so effective_timeout is non-None here.
    assert effective_timeout is not None
    result = await run_compile_file_with_state(
        file=file,
        workspace=workspace,
        timeout=effective_timeout,
        include_warnings=include_warnings,
        lifespan_state=lifespan_state,
        keep_vo=keep_vo,
        mode=mode,
        timing=timing,
    )
    return _finalize_tool_envelope(result, clamped=clamped, ws_warning=ws_warning)


# ---------------------------------------------------------------------------
# Tool: rocq_verify
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        title="Verify a proof matches its statement",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def rocq_verify(
    proof: str,
    problem_name: str,
    problem_statement: str,
    workspace: str = "",
    timeout: int = 0,
    include_warnings: bool = True,
    ctx: Context = None,
) -> dict[str, Any]:
    """Verify that a proof actually proves the original statement.

    Wraps the proof in a Module M sandbox and checks that the theorem
    matches the original problem_statement. Catches type redefinition,
    Admitted/Abort, custom axioms, and statement mismatches. Standard
    mathematical axioms (classical logic, Reals, etc.) are accepted.

    Run this after rocq_compile succeeds to confirm correctness.

    Args:
        proof: The complete proof file content (including imports).
        problem_name: The unqualified theorem name (e.g., "add_comm", not "Nat.add_comm").
        problem_statement: The original problem file content (with Admitted/Abort).
        workspace: Directory to use as workspace (default: ROCQ_WORKSPACE env var).
        timeout: Verification timeout in seconds (default: ROCQ_VERIFY_TIMEOUT env var).
        include_warnings: If True (default), include deduplicated warnings
            before the error in the output.  Set to False for compact errors.

    Returns the unified envelope ``{success, error, reason, ...}``.
    On failure, ``reason`` is one of:
        - ``"validation"``: invalid identifier, oversize source, malformed input.
        - ``"compile_error"``: the proof failed to compile.
        - ``"axiom_dependency"``: the proof relies on Admitted, ``admit``, or
          a custom (non-standard) axiom.
        - ``"type_mismatch"``: Phase 3 found that the proof's type differs
          from the problem's type.
        - ``"timeout"``: verification exceeded the budget across all phases.

    On success, ``assumptions`` and ``verification_method`` describe how
    the verdict was reached (``module_m``, ``shared_defs``, ``direct``).

    On ``pet_restarted: True`` (Phase 2 ``rocq_query`` path crashed pet
    while extracting shared definitions), call ``rocq_diag`` for memory
    headroom and recent error history.
    """
    resolved = _resolve_tool_envelope(
        tool="rocq_verify",
        ctx=ctx,
        workspace=workspace,
        timeout=timeout,
        timeout_default=config.ROCQ_VERIFY_TIMEOUT,
        ctx_optional=True,
    )
    if not isinstance(resolved, tuple):
        return resolved
    workspace, lifespan_state, ws_warning, clamped, effective_timeout = resolved

    # timeout_default was passed to _resolve_tool_envelope,
    # so effective_timeout is non-None here.
    assert effective_timeout is not None
    result = await run_verify(
        proof=proof,
        problem_name=problem_name,
        problem_statement=problem_statement,
        workspace=workspace,
        timeout=effective_timeout,
        include_warnings=include_warnings,
        lifespan_state=lifespan_state,
    )
    # Record verification failures (success=False with an error message)
    # so rocq_diag surfaces them.  Pet-level crashes routed through
    # run_verify -> _pet_runtime._run_with_pet (Phase 2 toc lookup) are already
    # recorded inside that helper, so skip when ``pet_restarted=True``
    # to avoid the double-record bug — the prior entry already carries
    # tool="rocq_verify" with the right reason because _extract_problem_structure
    # passes that tool name to _pet_runtime._run_with_pet.
    if (
        isinstance(result, dict)
        and result.get("success") is False
        and result.get("error")
        and not result.get("pet_restarted")
    ):
        _record_error(
            lifespan_state,
            "rocq_verify",
            str(result["error"]),
            reason=str(result.get("reason") or "validation"),
        )
    return _finalize_tool_envelope(result, clamped=clamped, ws_warning=ws_warning)


# ---------------------------------------------------------------------------
# Tool: rocq_query
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        title="Query the Rocq environment",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def rocq_query(
    command: str,
    preamble: str = "",
    file: str = "",
    workspace: str = "",
    max_results: int | None = None,
    include_warnings: bool = True,
    timeout: int = 0,
    from_state: int | None = None,
    ctx: Context = None,
) -> dict[str, Any]:
    """Search the Rocq environment — find lemmas, check types, inspect definitions.

    Does NOT modify any proof state. Use this to explore before proving:
      command="Search (nat -> nat -> nat)."  — find relevant lemmas
      command="Check Nat.add."               — check a term's type
      command="Print Nat.add."               — see a definition
      command="About plus."                  — summary of a name

    Three context modes (mutually exclusive in practice):
    - **preamble mode** (default): pass import / scope commands as a
      string.  Scope and import statements like ``Require Import``,
      ``From X Require Y``, ``Open Scope``, ``Set``, ``Unset``,
      ``Local``, and ``Section`` belong here — NOT inside ``command=``.
      ``command=`` runs each statement in isolation, so e.g. an
      ``Open Scope`` placed in ``command`` would not propagate to a
      following ``Search``.  See README "Recommended usage patterns →
      Imports and scopes in rocq_query".
    - **file mode**: pass a ``.v`` file path; the query runs with all
      definitions from that file in scope.  More reliable than preamble
      because it captures ``Open Scope``, ``Set`` options, etc., in the
      exact order the file declares them.
    - **from_state mode**: pass a ``state_id`` from a live ``rocq_check``
      session to query against the live proof context — opened scopes,
      hypotheses, and local definitions are all visible to ``Search`` /
      ``Print`` / ``About`` / ``Locate``.  The query runs against a
      transient child state which is discarded; the parent state is
      unchanged.  Canonical pattern::

          state_id = (await rocq_check(body=..., from_state=...))["state_id"]
          await rocq_query(command="Search _.", from_state=state_id)

      Prefer this over ``rocq_check(from_state=N, body="Search ...")``
      for pure queries — no new ``state_id`` is allocated and the
      state-table is not polluted.

    Args:
        command: The Rocq query command to execute.
        preamble: Optional import lines needed for the query context
                  (e.g., "Require Import Reals.\\nOpen Scope R_scope.").
        file: Path to a .v file (relative to workspace) whose definitions
            should be in scope. Mutually exclusive with preamble and
            from_state.
        workspace: Workspace directory.  If omitted, auto-detected by walking
            up from *file* looking for ``_RocqProject`` / ``_CoqProject`` /
            ``dune-project``; falls back to the ``ROCQ_WORKSPACE`` env var
            (default: cwd).
        max_results: Optional maximum number of results to return.
            Useful for broad Search patterns. If omitted, all results are
            returned (subject to character limit).
        include_warnings: If True (default), include all feedback returned
            by the query.  If False, drop entries at LSP Warning severity
            so warning noise does not crowd out tool output.
        timeout: Per-call timeout in seconds for expensive computations
            like ``Time Eval vm_compute in ...``.  ``0`` (default) means
            use ``ROCQ_PET_TIMEOUT``.  Clamped to ``ROCQ_QUERY_TIMEOUT_CAP``
            (default 300s); when clamping fires the response includes
            ``clamped_timeout: <cap>`` so the caller can diagnose unexpected
            timeouts.
        from_state: A live state_id (from ``rocq_start`` / ``rocq_check`` /
            ``rocq_step_multi``) to query against.  Mutually exclusive with
            *file*.  When set, *preamble* is ignored.

    On ``pet_restarted: True``, call ``rocq_diag`` for memory headroom and
    recent error history.
    """
    resolved = _resolve_tool_envelope(
        tool="rocq_query", ctx=ctx, workspace=workspace, file=file, timeout=timeout
    )
    if not isinstance(resolved, tuple):
        return resolved
    workspace, lifespan_state, ws_warning, clamped, effective_timeout = resolved

    result = await run_query(
        command=command,
        preamble=preamble,
        workspace=workspace,
        lifespan_state=lifespan_state,
        file=file,
        max_results=max_results,
        include_warnings=include_warnings,
        timeout=effective_timeout,
        from_state=from_state,
    )
    return _finalize_tool_envelope(result, clamped=clamped, ws_warning=ws_warning)


# ---------------------------------------------------------------------------
# Tool: rocq_assumptions
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        title="Audit a theorem's axioms",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def rocq_assumptions(
    name: str,
    file: str,
    workspace: str = "",
    timeout: int = 0,
    ctx: Context = None,
) -> dict[str, Any]:
    """List the axioms a theorem depends on.

    Runs ``Print Assumptions`` on the given theorem/lemma name and returns
    the resulting assumption list verbatim.  No classification is performed
    — this tool is pure introspection; the agent decides what's safe to
    trust.  Use ``rocq_verify`` for an admit-free / sandboxed trust
    decision on a candidate proof.

    The theorem must be defined in the given file.  The tool reads the file
    to set up the full Rocq environment (imports, scopes, definitions),
    ensuring the correct theorem is resolved even when names are reused
    across sections.

    Args:
        name: The theorem/lemma name to check (e.g., "add_comm").
        file: Path to the .v file where the theorem is defined (relative to workspace).
        workspace: Workspace directory.  If omitted, auto-detected by walking
            up from *file* looking for ``_RocqProject`` / ``_CoqProject`` /
            ``dune-project``; falls back to the ``ROCQ_WORKSPACE`` env var
            (default: cwd).
        timeout: Per-call timeout in seconds for the ``Print Assumptions``
            query.  Default 0 uses ``ROCQ_PET_TIMEOUT`` (env var, default
            30).  Raise this when the theorem's opaque-proof fetch from
            ``.vo`` files is slow.  Clamped to ``ROCQ_QUERY_TIMEOUT_CAP``
            (default 300s) so a stray large value cannot park the pet
            lock indefinitely; when clamping fires the response includes
            ``clamped_timeout: <cap>`` so the caller can diagnose
            unexpected timeouts.

            **Tip:** ``Print Assumptions`` triggers ``.vo`` opaque-proof
            fetching on first call (often 40+ modules on heavy library
            imports).  A pet restart from a timeout wipes Fleche, so a
            retry with the *same* timeout pays the same opaque-fetch
            cost from scratch and will time out again — the cost
            survives ``pet_restarted: True``.  Set ``timeout=`` high on
            the *first* call rather than relying on a retry after
            restart.

    Returns (key fields):
        success:     bool.
        theorem:     the cleaned theorem name.
        assumptions: list[str] of ``"name : type"`` pairs from
                     ``Print Assumptions``.  Empty when the theorem is closed
                     under the global context.  ``Print Assumptions`` does
                     not distinguish ``Admitted`` from ``Axiom`` / ``Parameter``
                     / ``Conjecture``, so admits and user axioms appear here
                     side-by-side.
        raw_output:  full raw ``Print Assumptions`` output.

    On theorem-not-found errors: response includes ``available_in_file:
    list[str]`` with the file's defined names (sorted, capped — see
    ``available_in_file_limit`` in the response when truncated).  When the
    file has more names than the cap, ``available_in_file_truncated:
    true``, ``available_in_file_total: <int>`` (uncapped count), and
    ``available_in_file_limit: <int>`` (the active cap) are also
    included; call ``rocq_toc`` for the full list.  Agents can fuzzy-
    match the requested name against this list to recover from typos.

    On ``pet_restarted: True``, call ``rocq_diag`` for memory headroom and
    recent error history.
    """
    resolved = _resolve_tool_envelope(
        tool="rocq_assumptions",
        ctx=ctx,
        workspace=workspace,
        file=file,
        timeout=timeout,
    )
    if not isinstance(resolved, tuple):
        return resolved
    workspace, lifespan_state, ws_warning, clamped, effective_timeout = resolved

    result = await run_assumptions(
        name=name,
        file=file,
        workspace=workspace,
        lifespan_state=lifespan_state,
        timeout=effective_timeout,
    )
    return _finalize_tool_envelope(result, clamped=clamped, ws_warning=ws_warning)


# ---------------------------------------------------------------------------
# Tool: rocq_toc
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        title="Outline a .v file",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def rocq_toc(
    file: str,
    workspace: str = "",
    timeout: int = 0,
    ctx: Context = None,
) -> dict[str, Any]:
    """Get the structure of a Rocq file: all definitions, lemmas, theorems, and sections.

    Returns a hierarchical outline showing what is defined in the file.
    Useful for understanding a file before working with it, or finding
    the name of a theorem to prove.

    Does NOT require a rocq_start session.

    Args:
        file: Path to the .v file (relative to workspace).
        workspace: Workspace directory.  If omitted, auto-detected by walking
            up from *file* looking for ``_RocqProject`` / ``_CoqProject`` /
            ``dune-project``; falls back to the ``ROCQ_WORKSPACE`` env var
            (default: cwd).
        timeout: Per-call timeout in seconds for the ``pet.toc`` lookup.
            Default 0 uses ``ROCQ_PET_TIMEOUT`` (env var, default 30).
            Raise this for very large files with heavy library imports.
            Clamped to ``ROCQ_QUERY_TIMEOUT_CAP`` (default 300s) so a
            stray large value cannot park the pet lock indefinitely;
            when clamping fires the response includes ``clamped_timeout:
            <cap>`` so the caller can diagnose unexpected timeouts.

    On ``pet_restarted: True``, call ``rocq_diag`` for memory headroom and
    recent error history.
    """
    resolved = _resolve_tool_envelope(
        tool="rocq_toc", ctx=ctx, workspace=workspace, file=file, timeout=timeout
    )
    if not isinstance(resolved, tuple):
        return resolved
    workspace, lifespan_state, ws_warning, clamped, effective_timeout = resolved

    result = await run_toc(
        file=file,
        workspace=workspace,
        lifespan_state=lifespan_state,
        timeout=effective_timeout,
    )
    return _finalize_tool_envelope(result, clamped=clamped, ws_warning=ws_warning)


# ---------------------------------------------------------------------------
# Tool: rocq_notations
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        title="Explain notations in a statement",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def rocq_notations(
    statement: str,
    preamble: str = "",
    workspace: str = "",
    timeout: int = 0,
    ctx: Context = None,
) -> dict[str, Any]:
    """List all notations in a Rocq statement and how they resolve.

    Helps debug notation ambiguity (e.g., which scope does "+" resolve to?
    Is "=" Leibniz equality or Qeq?).

    Pass the statement part of a Lemma/Theorem declaration (after the colon).
    For example, for "Lemma foo : forall n, n + 0 = n", pass
    statement="forall n, n + 0 = n".

    NOTE: Only works on statements (propositions/types), not arbitrary terms.

    Args:
        statement: The proposition/type to analyze.
        preamble: Import lines for context (e.g., "Require Import QArith.").
        workspace: Workspace directory (default: ROCQ_WORKSPACE env var).
        timeout: Per-call timeout in seconds for the notation lookup.
            Default 0 uses ``ROCQ_PET_TIMEOUT`` (env var, default 30).
            Raise this for statements that require heavy library imports.
            Clamped to ``ROCQ_QUERY_TIMEOUT_CAP`` (default 300s) so a
            stray large value cannot park the pet lock indefinitely;
            when clamping fires the response includes ``clamped_timeout:
            <cap>`` so the caller can diagnose unexpected timeouts.

    On ``pet_restarted: True``, call ``rocq_diag`` for memory headroom and
    recent error history.
    """
    resolved = _resolve_tool_envelope(
        tool="rocq_notations", ctx=ctx, workspace=workspace, timeout=timeout
    )
    if not isinstance(resolved, tuple):
        return resolved
    workspace, lifespan_state, ws_warning, clamped, effective_timeout = resolved

    result = await run_notations(
        statement=statement,
        preamble=preamble,
        workspace=workspace,
        lifespan_state=lifespan_state,
        timeout=effective_timeout,
    )
    return _finalize_tool_envelope(result, clamped=clamped, ws_warning=ws_warning)


# ---------------------------------------------------------------------------
# Tool: rocq_start
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        # destructiveHint covers the force_restart=True path, which kills
        # pet and clears the state table for every caller of this server.
        title="Start or resume a proof session",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    )
)
async def rocq_start(
    file: str = "",
    theorem: str = "",
    workspace: str = "",
    line: int | None = None,
    character: int | None = None,
    preamble: str = "",
    force_restart: bool = False,
    timeout: int = 0,
    ctx: Context = None,
) -> dict[str, Any]:
    """Start an interactive proof session — see goals, explore tactics.

    Returns a state_id for use with rocq_check and rocq_step_multi.
    Also returns the current proof goals at the starting position,
    so this tool can be used to inspect goals at any point in a file.
    For a position inside a proof, the response also carries
    ``focus_depth`` — how many ``{...}`` / bullet focus frames are open
    above the goal (0 at the top level) — so a session resumed mid-proof
    knows its bullet nesting (omitted for preamble-only starts).

    Three start modes (precedence: theorem > position > preamble):
    1. By theorem: file + theorem — start proving a specific theorem.
       Tip: for a scratch file under ``/tmp`` that needs the project's
       load path, keep the file name stable across iterations (e.g.
       ``/tmp/probe.v``) — Fleche caches per file path, so rotating
       probe names defeats the warmth.
    2. By position: file + line + character — jump to any position in
       a file and see the proof goals there.  Useful for inspecting
       proof state at a specific point, or recovering from an error
       position returned by rocq_compile.
    3. From imports: preamble — set up an import context only.
       **Preferred for scratch iteration** (no project files needed,
       preferred over ``coqc /tmp/foo.v``): call
       ``rocq_start(preamble='Require Import ...')`` once, then iterate
       with rocq_check / rocq_step_multi against the returned state_id.
       The import set is content-hashed and warm across iterations even
       if you change the lemma body (see
       ``interactive.py:_get_or_create_import_state``).

    **Position semantics (mode 2):** ``line`` and ``character`` are
    0-indexed.  Petanque resolves the cursor to a sentence boundary by
    *rounding forward* through the sentence that contains the cursor:

    - Cursor on any character of a sentence — its first letter, any
      character inside, or its terminating period — yields the state
      **after** that whole sentence has executed.
    - Cursor in the whitespace **before** a sentence's first
      non-whitespace character yields the state **before** that
      sentence (= after the previous sentence).
    - Cursor in the whitespace **after** a sentence's terminating
      period yields the state **after** that sentence.

    So to inspect goals **before** a tactic, point at the whitespace
    just before its first character.  To inspect goals **after** a
    tactic, point at any character of the tactic (including its
    period) or at the whitespace immediately following the period.

    **Important:** The interactive session reads the file at start time and
    does not track subsequent edits. If another process or agent modifies the
    file while a session is active, the proof state becomes stale and tactics
    may fail or produce wrong results. To avoid this, work on a **copy** of
    the file for interactive proving, or restart the session after edits.

    Args:
        file: Path to the .v file (relative to workspace).
        theorem: Name of the theorem to prove.
        workspace: Workspace directory.  If omitted, auto-detected by walking
            up from *file* looking for ``_RocqProject`` / ``_CoqProject`` /
            ``dune-project``; falls back to the ``ROCQ_WORKSPACE`` env var
            (default: cwd).
        line: 0-based line number for position-based start.  See
            "Position semantics" above for how the cursor is resolved
            to a sentence boundary.
        character: 0-based character offset for position-based start.
            See "Position semantics" above.
        preamble: Import commands for preamble mode (e.g., "Require Import Lia.").
        force_restart: If True, kill pet, clear the state table, and
            respawn before starting.  Recovery primitive for the rare
            cases where the shared pet is in a bad state: accumulated
            RAM bloat after long shared use, indexing corruption, or a
            "State N expired" that repeats after a plain ``rocq_start``
            retry (suggesting a peer caller is also force-restarting).
            Actively-used states survive peer churn via LRU eviction —
            ``force_restart=True`` is *not* needed as routine insurance
            and is unhelpful when a recent response already carried
            ``pet_restarted: True`` (pet is already fresh).  See README
            "Concurrency model".  Default: False.
        timeout: Per-call timeout in seconds for opening the session.
            Default 0 uses ``ROCQ_PET_TIMEOUT`` (env var, default 30).
            Raise this for files with heavy library imports.
            Clamped to ``ROCQ_QUERY_TIMEOUT_CAP`` (default 300s) so a stray
            large value cannot park the pet lock indefinitely; when
            clamping fires the response includes ``clamped_timeout:
            <cap>`` so the caller can diagnose unexpected timeouts.

    On theorem-not-found errors: response includes ``available_in_file:
    list[str]`` with the file's defined names (sorted, capped — see
    ``available_in_file_limit`` in the response when truncated).  When the
    file has more names than the cap, ``available_in_file_truncated:
    true``, ``available_in_file_total: <int>`` (uncapped count), and
    ``available_in_file_limit: <int>`` (the active cap) are also
    included; call ``rocq_toc`` for the full list.  Agents can fuzzy-
    match the requested name against this list to recover from typos.

    On ``pet_restarted: True``, call ``rocq_diag`` for memory headroom and
    recent error history.
    """
    resolved = _resolve_tool_envelope(
        tool="rocq_start", ctx=ctx, workspace=workspace, file=file, timeout=timeout
    )
    if not isinstance(resolved, tuple):
        return resolved
    workspace, lifespan_state, ws_warning, clamped, effective_timeout = resolved

    result = await run_start(
        file=file,
        theorem=theorem,
        workspace=workspace,
        lifespan_state=lifespan_state,
        line=line,
        character=character,
        preamble=preamble,
        force_restart=force_restart,
        timeout=effective_timeout,
    )
    return _finalize_tool_envelope(result, clamped=clamped, ws_warning=ws_warning)


# ---------------------------------------------------------------------------
# Tool: rocq_step_multi
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        title="Try tactics without committing",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def rocq_step_multi(
    tactics: list[str],
    from_state: int,
    include_warnings: bool = True,
    timeout: int = 0,
    ctx: Context = None,
) -> dict[str, Any]:
    """Try multiple tactics at once — find what works without guessing.

    Tests each tactic against a specific proof state and returns all
    results. Does NOT advance the state — commit the winner with
    rocq_check.

    Use this whenever you're unsure which tactic to apply:
      tactics=["auto.", "lia.", "lra.", "ring.", "tauto.", "firstorder."]

    Or to auto-solve a subgoal, try the standard automation battery:
      tactics=["trivial.", "reflexivity.", "assumption.", "exact I.",
               "auto.", "eauto.", "tauto.", "intuition.", "lia.", "lra.",
               "nia.", "nra.", "ring.", "field.", "decide equality.",
               "firstorder."]
    Note: lia/lra/ring/field require the .v file to import Lia/Lra/Ring/Field.

    Or to explore proof structure:
      tactics=["destruct n.", "induction n.", "case_eq n."]

    Each result entry includes a ``feedback`` field (truncated string)
    when the tactic produces visible output (e.g., ``Print``, ``Search``).
    Each *successful* entry also carries a ``focus_depth`` field — how
    many ``{...}`` / bullet focus frames that tactic leaves open above
    the goal (0 at the top level).

    **Canonical exploration pattern:** if the first few steps of a proof
    are a confident prefix, advance with ``rocq_check`` first and pass
    the resulting ``state_id`` as ``from_state`` here — don't repeat the
    prefix inside every entry of ``tactics``.  See README
    "Recommended usage patterns → Multi-tactic exploration".

    Args:
        tactics: List of tactics to try (max 20).
        from_state: State ID to try the tactics from.  Required — use the
            ``state_id`` returned by ``rocq_start`` or a previous
            ``rocq_check``.  There is no implicit "current state"
            fallback (avoids cross-agent state confusion when peers
            share this rocq-mcp process).
        include_warnings: If True (default), per-tactic ``feedback`` includes
            all severities.  If False, drop entries at LSP Warning severity.
        timeout: Per-call timeout in seconds for the whole batch.  The
            per-tactic budget is ``timeout / len(tactics)`` (subject to the
            usual ``Timeout`` eligibility rules).  Default 0 uses
            ``ROCQ_PET_TIMEOUT`` (env var, default 30).  Raise this when
            individual tactics in the batch are expensive.  Clamped to
            ``ROCQ_QUERY_TIMEOUT_CAP``
            (default 300s) so a stray large value cannot park the pet
            lock indefinitely; when clamping fires the response includes
            ``clamped_timeout: <cap>`` so the caller can diagnose
            unexpected timeouts.

    On ``pet_restarted: True``, call ``rocq_diag`` for memory headroom and
    recent error history.
    """
    if ctx is None:
        return _no_ctx_fail("rocq_step_multi")

    effective_timeout, clamped = _resolve_call_timeout(timeout)

    result = await run_step_multi(
        tactics=tactics,
        lifespan_state=ctx.lifespan_context,
        from_state=from_state,
        include_warnings=include_warnings,
        timeout=effective_timeout,
    )
    if clamped:
        result["clamped_timeout"] = config.ROCQ_QUERY_TIMEOUT_CAP
    return result


# ---------------------------------------------------------------------------
# Tool: rocq_check
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        # Additive, never destructive: allocates a fresh state_id and
        # leaves existing states untouched.  Not idempotent (each call
        # creates a new state).
        title="Run proof commands from a state",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    )
)
async def rocq_check(
    body: str,
    from_state: int,
    workspace: str = "",
    timeout: int = 0,
    include_warnings: bool = True,
    ctx: Context = None,
) -> dict[str, Any]:
    """Run proof commands from cached imports — fast iterative checking.

    Much faster than rocq_compile for iterative proof development:
    imports are cached (first call processes them, subsequent calls skip), and on error
    returns the last valid state for immediate interactive recovery
    via rocq_check(from_state=...) or rocq_step_multi(from_state=...).

    When proof_finished=True, also returns proof_tactics (ordered list of
    all tactics from root to current state) and proof_hint (instructions
    for assembling the final .v file).  On a broken walk (an ancestor
    state was LRU-evicted, or a cycle was detected), proof_tactics and
    proof_hint are omitted; the response carries ``proof_tactics_status``
    (``"ancestor_evicted"`` or ``"cycle"``), ``proof_tactics_broken_at``
    (the state id where the walk gave up), and ``proof_tactics_hint``
    instead — clients that ignore these keys see no half-chain at all.

    Recommended workflow (each step threads ``state_id`` explicitly):
    1. ``s0 = rocq_start(file=..., theorem=...)["state_id"]``
    2. ``s1 = rocq_check(from_state=s0, body="intros. simpl.")["state_id"]``
    3. If stuck: ``rocq_step_multi(from_state=s1, tactics=[...])`` to explore
    4. ``rocq_check(from_state=s1, body="winning_tactic.")`` to commit

    When commands produce visible output (e.g., ``Print``, ``Check``,
    ``vm_compute``, ``native_compute``), a ``feedback`` field is included
    as a list of ``[command, output]`` pairs (truncated per step at 50K
    chars).  Omitted when no command produces output.

    When proof goals are available, the success response carries
    ``focus_depth`` — how many ``{...}`` / bullet focus frames are
    currently open above the goal (0 at the top level) — so an agent
    stepping through nested subgoals can tell how deep its focus nesting
    is.  (Omitted on empty-body checks and when goal state cannot be
    retrieved, like the other goal-derived fields.)

    **Note:** If the underlying .v file is modified after rocq_start, the
    session state becomes stale. A ``stale_warning`` field is returned when
    this is detected. Restart the session with rocq_start after file edits.

    Args:
        body: Commands to execute (one or more Rocq sentences).
        from_state: State ID to execute from.  Required — use the
            ``state_id`` returned by ``rocq_start`` or a previous
            ``rocq_check``.  There is no implicit "current state"
            fallback (avoids cross-agent state confusion when peers
            share this rocq-mcp process).
        workspace: Accepted for API compatibility but unused; the
            active workspace comes from the state entry set by
            ``rocq_start``.
        timeout: Per-call timeout in seconds for the batch of commands.
            Default 0 uses ``ROCQ_PET_TIMEOUT`` (env var, default 30).
            Raise this for compute-heavy tactics (``vm_compute``,
            ``native_compute``).  Clamped to ``ROCQ_QUERY_TIMEOUT_CAP``
            (default 300s) so a stray large value cannot park the pet
            lock indefinitely; when clamping fires the response includes
            ``clamped_timeout: <cap>`` so the caller can diagnose
            unexpected timeouts.
        include_warnings: If True (default), per-step ``feedback`` includes
            all severities.  If False, drop entries at LSP Warning severity.

    On ``pet_restarted: True``, call ``rocq_diag`` for memory headroom and
    recent error history.
    """
    # Note: workspace param is accepted for API compatibility but unused;
    # the active workspace comes from the state entry set by rocq_start.
    effective_timeout, clamped = _resolve_call_timeout(timeout)

    if ctx is None:
        return _no_ctx_fail("rocq_check")

    result = await run_check(
        body=body,
        lifespan_state=ctx.lifespan_context,
        from_state=from_state,
        timeout=effective_timeout,
        include_warnings=include_warnings,
    )
    if clamped and isinstance(result, dict):
        result["clamped_timeout"] = config.ROCQ_QUERY_TIMEOUT_CAP
    return result


@mcp.tool(
    annotations=ToolAnnotations(
        title="Server runtime diagnostics",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def rocq_diag(ctx: Context = None) -> dict[str, Any]:
    """Operational diagnostics: pet health, memory headroom, recent errors.

    Use this when:
    - A tool returned ``pet_restarted: True`` and you want to see what
      happened.
    - You're considering a long ``vm_compute`` and want to check memory
      headroom against ``max_rss_mb_threshold``.
    - You want to know which proof states are currently live in pet's
      state table.
    - You suspect another agent is sharing this rocq-mcp process.
      Heuristic: compare ``live_states[*].file`` against the files you
      opened this session — entries you did not create signal a foreign
      caller (works only when agents are on disjoint files).  Actively
      used states are LRU-protected against peer churn, so sharing is
      mostly a latency and RAM concern; if repeated state expiries
      survive that floor, ``rocq_start(..., force_restart=True)`` is
      the recovery — see README "Concurrency model".

    Does NOT spawn pet if it's not running; just reports state.

    Response shape:

    - ``server_version``: the rocq-mcp package version (from
      ``importlib.metadata``).  Include in bug reports.
    - ``pet``: ``{pid, uptime_seconds, restarts, generation}``
    - ``memory``: ``{pet_rss_mb, peak_pet_rss_mb, max_rss_mb_threshold,
      sample_status}`` where ``sample_status`` is one of ``"ok"`` /
      ``"no_pet"`` / ``"psutil_error"`` and disambiguates a ``None``
      ``pet_rss_mb``.
    - ``load_average``: ``{"1m": float, "5m": float, "15m": float}`` —
      kernel-tracked system load averages over the last 1, 5, and 15
      minutes (from ``os.getloadavg()``).  ``None`` on platforms without
      an equivalent (e.g. Windows).  Use this to disambiguate CPU
      contention from tactic divergence when a timeout fires.
    - ``live_states``: capped at 50 most-recent entries (by
      ``created_at``) to keep the payload bounded.  Each entry has
      ``{state_id, parent, file, theorem, age_seconds}``.
    - ``live_states_total``: full count of entries in the state table
      (use this to detect that ``live_states`` was truncated).
    - ``recent_errors``: ring buffer of the last 20 errors, each
      ``{tool, message, reason, ago_seconds}``.  ``reason`` is one of:

      - **Pet-side** (set by ``_run_with_pet``): ``"timeout"``,
        ``"crashed"``, ``"memory_exhausted"``, ``"lock_contended"``,
        ``"unavailable"``.
      - **Validation / lookup** (set by tools before pet): ``"validation"``,
        ``"not_found"`` (rocq_start / rocq_assumptions on a typo).
      - **rocq_check mid-batch**: ``"tactic_failed"`` (a tactic was
        rejected by Coq — distinct from a transport-level ``"crashed"``).
      - **rocq_verify-specific**: ``"compile_error"``,
        ``"axiom_dependency"``, ``"type_mismatch"``.

      The full set is :data:`_RECENT_ERROR_REASONS`.
    """
    if ctx is None:
        return _no_ctx_fail("rocq_diag")
    return _build_diag_snapshot(ctx.lifespan_context)


# ---------------------------------------------------------------------------
# Tool: rocq_health
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        title="Toolchain health check",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def rocq_health(ctx: Context = None) -> dict[str, Any]:
    """Health check: is the server OK, and which Rocq/opam switch is it on?

    Call this first when something looks wrong (e.g. a proof that built
    yesterday now fails, or ``coqc`` behaves like a different version than
    your shell) — an MCP server inherits its ``PATH`` / opam environment
    from whatever launched it, which can differ from your interactive
    shell.  This tool reports the toolchain the server *actually* resolves.

    Read-only: does NOT spawn pet (mirrors ``rocq_diag``).  Use
    ``rocq_diag`` for runtime health (memory, live states, recent errors);
    use this for *toolchain* health (which binaries / which switch).

    Response shape:

    - ``ok``: ``True`` when ``coqc`` resolves on ``PATH`` (the server can
      do its core job).  Interactive capability is reported separately under
      ``toolchain.pet`` so a coqc-only deployment still reads as healthy.
    - ``server_version``: the rocq-mcp package version.
    - ``switch``: the active switch name (e.g. ``"rocq9"``), or the project
      path for a local ``_opam`` switch, or ``None`` for a non-opam install.
    - ``switch_prefix``: the resolved ``OPAM_SWITCH_PREFIX``-style path
      (``None`` when no switch was identified).
    - ``switch_is_local``: ``True`` for a project-local (``_opam``) switch.
    - ``switch_source``: how the switch was determined — ``"opam_env"``
      (from ``$OPAM_SWITCH_PREFIX``, authoritative), ``"binary_path"``
      (inferred from the resolved ``coqc`` path), or ``"unknown"``
      (non-opam / unrecognised layout).
    - ``toolchain``: ``{coqc: {path, version}, pet: {path, version,
      pytanque_importable}}`` — the binaries the server resolves and their
      versions (``None`` when a binary is missing).
    - ``pet``: ``{running, pid}`` — whether the pet subprocess is currently
      live (not spawned by this call).
    - ``warnings``: human-readable notes (missing ``coqc`` / ``pet`` /
      pytanque, or a switch that could not be identified).

    To change switch, see ``rocq_switch`` — and note the caveats there:
    live ``state_id``s and previously-built ``.vo`` artifacts do not carry
    across a switch change.
    """
    if ctx is None:
        return _no_ctx_fail("rocq_health")
    # build_health_snapshot probes `coqc --print-version` / `pet --version`
    # via subprocess; run off the event-loop thread.
    return await asyncio.to_thread(build_health_snapshot, ctx.lifespan_context)


# ---------------------------------------------------------------------------
# Tool: rocq_switch
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        # Process-global: kills pet, clears every caller's state table,
        # and may leave .vo artifacts ABI-incompatible.
        title="Change opam switch (process-global)",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=False,
    ),
    meta={"anthropic/requiresUserInteraction": True},
)
async def rocq_switch(name: str = "", ctx: Context = None) -> dict[str, Any]:
    """Switch the running server to a different opam switch, in-session.

    Resolves *name* via ``opam env --switch <name> --set-switch``, applies
    the resulting environment to the live server process, and kills the pet
    subprocess so the next pet-routed call respawns under the new switch.
    Subsequent ``coqc`` invocations resolve the new switch's binary too.

    **This is a sharp tool — read before use:**

    - **All live ``state_id``s are discarded.** The pet state table is
      cleared (a state from the old switch is meaningless under the new
      one). Any in-flight ``rocq_start`` / ``rocq_check`` session must be
      restarted with ``rocq_start`` after switching.
    - **``.vo`` artifacts may be ABI-incompatible.** Files compiled under
      the old switch may fail to ``Require`` under the new one
      (``Compiled library ... makes inconsistent assumptions``). Recompile
      dependencies after switching.
    - **Process-global.** The change affects the whole server process, so
      *every* agent sharing this rocq-mcp instance is moved to the new
      switch. Do not use in a shared multi-agent setup without coordination:
      call ``rocq_diag`` first and check its ``live_states`` to confirm no
      peer has an in-flight session before switching.
    - For a stable per-deployment switch, prefer pinning it at launch in the
      MCP client config (e.g. ``opam exec --switch=<name> -- ...`` as the
      server ``command``) rather than switching at runtime.

    Args:
        name: The opam switch to activate (e.g. ``"rocq9"``). Must be an
            installed switch; see ``rocq_health`` / ``opam switch list``.

    On success returns the post-switch ``rocq_health`` snapshot, augmented
    with ``switched: True``, ``previous_switch``, and a ``note`` describing
    the invalidation. Failure envelopes (``{success: False, reason, error}``):

    - ``reason="not_found"`` — ``name`` is not an installed switch; the
      response carries ``available_switches: list[str]`` for typo recovery.
    - ``reason="validation"`` — empty ``name``, or opam is unavailable /
      its ``opam env`` output could not be parsed (the installed-switch
      list was unavailable, so the name could not be classified).
    - ``reason="lock_contended"`` — pet was busy and the switch was not
      applied; retry once in-flight calls settle.
    """
    if ctx is None:
        return _no_ctx_fail("rocq_switch")
    lifespan_state = ctx.lifespan_context

    if not name or not name.strip():
        return _fail(
            lifespan_state,
            "rocq_switch",
            "rocq_switch requires a non-empty `name` (the opam switch to "
            "activate, e.g. 'rocq9').",
            reason="validation",
        )
    name = name.strip()

    previous = _detect_switch(_resolve_binary(config.ROCQ_COQC_BINARY))["switch"]

    env, error = await asyncio.to_thread(compute_switch_env, name)
    if env is None:
        extra: dict[str, Any] = {}
        switches = await asyncio.to_thread(list_switches)
        name_unknown = switches is not None and name not in switches
        if name_unknown:
            extra["available_switches"] = switches
        # A syntactically-fine name that does not resolve to an installed
        # switch is a name-resolution failure ("not_found", paired with
        # available_switches for typo recovery), matching rocq_start.  The
        # empty-name guard above stays "validation" (input-shape).
        return _fail(
            lifespan_state,
            "rocq_switch",
            error or "switch change failed",
            reason="not_found" if name_unknown else "validation",
            **extra,
        )

    # Apply the new environment + kill pet under _pet_runtime._pet_lock so the mutation
    # cannot interleave with a concurrent _pet_runtime._ensure_pet spawn (which holds the
    # same lock and would otherwise adopt a pet started on the OLD env, then
    # survive our _pet_runtime._invalidate_pet on a live process — no broken pipe, no
    # respawn).  Serializing here guarantees the next spawn sees the new env.
    # The semaphore matches every other pet-mutating path's `async with sem`.
    lock_timeout = (lifespan_state.get("pet_timeout") or config.ROCQ_PET_TIMEOUT) * 0.8

    def _apply_switch() -> bool:
        lock = (
            _pet_runtime._pet_lock
        )  # local ref survives a _pet_runtime._force_release_pet_lock swap
        if not lock.acquire(timeout=lock_timeout):
            return False
        try:
            # Apply the new switch's env, then drop any switch-scoped keys the
            # new switch does NOT set.  update() alone only overwrites keys
            # present in the new env; a key the old switch set but the new one
            # omits (e.g. a stale CAML_LD_LIBRARY_PATH / OPAM_LAST_ENV) would
            # otherwise linger and mis-resolve a later lookup.  Compute both
            # sets first so the mutation window a lock-free reader (rocq_health)
            # can observe stays as narrow as possible.
            new_env = {k: env[k] for k in _SWITCH_ENV_KEYS if k in env}
            stale = [k for k in _SWITCH_ENV_KEYS if k not in env and k in os.environ]
            os.environ.update(new_env)
            for key in stale:
                del os.environ[key]
            # Kill pet + clear the state table + invalidate the import cache
            # (via _pet_runtime._pet_invalidation_hooks).  The next pet-routed call lazily
            # respawns pet under the new environment.
            _pet_runtime._invalidate_pet(lifespan_state)
            return True
        finally:
            lock.release()

    async with _pet_runtime._get_pet_semaphore():
        applied = await asyncio.to_thread(_apply_switch)
    if not applied:
        return _fail(
            lifespan_state,
            "rocq_switch",
            "rocq_switch: pet is busy (lock contention); the switch was not "
            "applied. Retry once in-flight pet calls settle.",
            reason="lock_contended",
        )

    snapshot = await asyncio.to_thread(build_health_snapshot, lifespan_state)
    snapshot["switched"] = True
    snapshot["previous_switch"] = previous
    snapshot["note"] = (
        f"Switched to {snapshot['switch']!r}. pet was killed and the state "
        "table cleared — restart any interactive session with rocq_start. "
        ".vo artifacts built under the previous switch may be "
        "ABI-incompatible; recompile dependencies as needed."
    )
    return snapshot


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the MCP server."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
