# Changelog

All notable changes to `rocq-mcp` are documented here.  The project
uses a per-feature-branch workflow: each entry below references the
originating branch so the change can be cross-referenced against the
PR / integration commit.

## Unreleased — pending integration

The following branches exist off `main` and have not yet landed.  They
will be merged through an integration branch.

**Compatibility notes** (soft, additive — clients should adapt):

- `proof_tactics` may now contain a sentinel string at index 0 when
  `proof_tactics_complete: False`
  (`(* ... earlier tactics lost — chain broken at state N *)`).
  Clients that programmatically count tactic length will be off-by-one
  in the truncated case; gate on `proof_tactics_complete` first.
- `rocq_step_multi` result entries now always include `time_ms` (an
  int, wall-clock ms).  Strict-shape clients should accept the
  additional key on both success and `tactic_failed` outcomes.

### Added

- **Per-call timeout on `rocq_start` and `rocq_step_multi`**
  (`feat/per-call-timeout-start-step-multi`).  New optional `timeout=`
  argument on both tools, default `0` (falls back to
  `ROCQ_PET_TIMEOUT`).  Mirrors the pattern already on `rocq_check` /
  `rocq_compile` / `rocq_verify`.  Useful for projects with heavy
  import chains that exceed the 30 s default.

- **Per-call timeout on `rocq_assumptions`, `rocq_query` (file mode),
  `rocq_toc`, `rocq_notations`** (`feat/per-call-timeout-extended`).
  Extends the same pattern to the remaining file-loading tools.

- **Per-tactic timeouts and timing on `rocq_step_multi`**
  (`feat/step-multi-per-tactic-timeouts-and-timing`).  New optional
  `timeouts: list[float]` argument; its length must equal
  `len(tactics)` and each tactic uses its own budget.  Each entry in
  the response also carries a `time_ms` field — wall-clock
  milliseconds for the underlying pet call, present on both success
  and `tactic_failed` entries.

- **Bullet / focus payload on `rocq_check`**
  (`feat/bullet-focus-payload`).  When the body is a single
  bullet/focus token (`{`, `}`, or a run of `-`/`+`/`*`), the
  response now carries `focus_command`, `goals_before`,
  `goals_after`, `focus_depth_before`, `focus_depth_after` so the
  agent can see the focus transition rather than an empty `goals`
  string.  Additive — non-bullet bodies are unaffected.

- **`rocq_goal_at` and `rocq_hover`** (`feat/file-anchored-tools`).
  Two new stateless file-anchored read tools that query Rocq at
  `(file, line, character)` without allocating an entry in the LRU
  state table.  Both responses carry `stateless: True`.
  `rocq_hover` returns best-effort `name`, `kind`, `type`,
  `qualified_name`, `definition_file`, plus raw `Locate` / `About`
  output.

### Fixed

- **Broken `proof_tactics` chain is now visible**
  (`fix/proof-tactics-broken-chain-sentinel`).  When
  `_reconstruct_tactic_path` walks a parent chain that has been
  truncated by LRU eviction or a cycle, the returned list now starts
  with a sentinel comment:
  `(* ... earlier tactics lost — chain broken at state N *)`.
  Keeps the output well-formed if pasted into a `.v` file and makes
  the truncation actionable.  `proof_tactics_complete: False` is
  unchanged.

- **Stale `.vo` warning in `rocq_compile_file`**
  (`fix/compile-file-stale-vo-warning`).  `coqc <file>` happily
  reuses pre-existing `.vo` files for transitively imported modules.
  The response now carries a `stale_dependencies: {files, count,
  truncated, advisory}` field whenever `.v` files in the workspace
  are newer than their sibling `.vo`.  Advisory only — the compile
  result itself is unchanged.

## 0.2.2 — 2026-05-14

- Pin pytanque to v0.2.2 (#19).

## 0.2.1 — 2026-05-13

- Round 3: response-shape fixes and final envelope consolidation.
- Round 2: tighten envelope contract and close soundness gaps.
- Round 1: address Phase 1 audit findings.
