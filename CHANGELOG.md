# Changelog

All notable changes to `rocq-mcp` are documented here.  The project
uses a per-feature-branch workflow: each entry below references the
originating branch so the change can be cross-referenced against the
PR / integration commit.

## Unreleased — pending integration

The following branches exist off `main` and have not yet landed.  They
will be merged through an integration branch.

**Compatibility notes** (soft, additive — clients should adapt):

- When a finished proof's tactic chain cannot be reconstructed (an
  ancestor state was LRU-evicted, or a cycle was detected),
  `rocq_check` now omits `proof_tactics` / `proof_hint` and instead
  carries `proof_tactics_status` (`"ancestor_evicted"` or `"cycle"`),
  `proof_tactics_broken_at`, and `proof_tactics_hint`.  Clients that
  consume `proof_tactics` should gate on its presence before reading
  it so they never render a partial walk as a finished proof.

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

### Fixed

- **Broken `proof_tactics` chain is now visible**
  (`fix/proof-tactics-broken-chain`).  When
  `_reconstruct_tactic_path` walks a parent chain that has been
  truncated by LRU eviction or a cycle, the walk cannot complete; the
  response now omits `proof_tactics` / `proof_hint` and carries
  `proof_tactics_status` (`"ancestor_evicted"` or `"cycle"`),
  `proof_tactics_broken_at` (the state id where the walk gave up),
  and a short `proof_tactics_hint` instead.  Clients that ignore
  these keys see no half-chain — they never render a partial walk as
  a finished proof.

## 0.2.2 — 2026-05-14

- Pin pytanque to v0.2.2 (#19).

## 0.2.1 — 2026-05-13

- Round 3: response-shape fixes and final envelope consolidation.
- Round 2: tighten envelope contract and close soundness gaps.
- Round 1: address Phase 1 audit findings.
