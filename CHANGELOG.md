# Changelog

## Unreleased (0.4.0)

### Response-shape changes (read this if you parse tool responses)

- **`rocq_assumptions` no longer returns `raw_output` by default.** The
  parsed `assumptions` list carries the same information; pass
  `include_raw=true` for the verbatim `Print Assumptions` text. Parse
  failures still include `raw_output` unconditionally (there it is the
  payload).
- **`rocq_step_multi` deduplicates identical outcomes.** The first
  tactic reaching a proof state carries the full payload; later tactics
  reaching the same state return
  `{tactic, success, proof_finished, same_outcome_as: <index>}` without
  repeating goals. Responses gain `distinct_outcomes`. Bounds the
  worst case from ~160KB to one goals payload per distinct outcome.
- **`proof_hint` shortened** (still starts with "Proof complete").

### Added

- `goals_format` on `rocq_check` (`pretty` | `structured` |
  `names_only` | `diff` | `none`) and on `rocq_start` /
  `rocq_step_multi` (`pretty` | `structured` | `names_only`).
  `structured` exposes pytanque's hypothesis structure
  (`{hyps: [{names, type, body?}], conclusion}`); `diff` returns
  `goals_diff` (delta vs the `from_state` parent) instead of full
  goals — typically 50–85% smaller on the iteration hot loop.
- Server instructions, tool annotations (7 tools read-only), and
  `version` published over MCP.
- Four documentation resources (`rocq://guide/{workflows,failures,
  concurrency,responses}`) and two prompts (`prove_theorem`,
  `debug_compile_error`).
- `degraded: ["<field>:<code>"]` on responses whose best-effort
  enrichment failed (`ROCQ_DEBUG_ENRICHMENT=1` adds detail);
  `enrichment_failures` counters and `lock` contention telemetry in
  `rocq_diag`.
- Tier-1 eval harness (`evals/`), ruff, pytest-timeout, 3.12/3.13 CI
  job, generated README tools table.

### Changed

- Tool descriptions rewritten as ≤2KB contracts; deep reference moved
  to the guide resources. Full `tools/list` payload: 45.6K → 21.8K
  chars.
- Failure-reason taxonomy single-sourced in `rocq_mcp.taxonomy`
  (wire strings unchanged, pinned by tests).
