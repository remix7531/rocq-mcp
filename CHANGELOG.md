# Changelog

## Unreleased (0.4.0)

### Added

- Server instructions, tool annotations (7 tools read-only), and
  `version` published over MCP.
- Four documentation resources (`rocq://guide/{workflows,failures,
  concurrency,responses}`) and two prompts (`prove_theorem`,
  `debug_compile_error`).
- `degraded: ["<field>:<code>"]` on responses whose best-effort
  enrichment failed (`ROCQ_DEBUG_ENRICHMENT=1` adds detail);
  `enrichment_failures` counters and `lock` contention telemetry in
  `rocq_diag`.

### Changed

- Tool descriptions rewritten as ≤2KB contracts; deep reference moved
  to the guide resources. Full `tools/list` payload roughly halved
  (~46K → ~22K chars).
- Failure-reason taxonomy single-sourced in `rocq_mcp.taxonomy`
  (wire strings unchanged, pinned by tests).

### Internal

- `server.py` decomposed (2,929 → ~1,700 lines; zero behavior change):
  workspace/path/dune logic → `workspace.py`, pet lifecycle + lock +
  watchdog + `_run_with_pet` → `pet_runtime.py`. The
  server↔submodule circular import is eliminated
  (domain modules import config/taxonomy/envelope/workspace/pet_runtime
  only).
  Monkeypatch targets moved with the code: pet lifecycle patches on
  `rocq_mcp.pet_runtime`, workspace helpers on `rocq_mcp.workspace`,
  `ROCQ_*` knobs on `rocq_mcp.config`.
