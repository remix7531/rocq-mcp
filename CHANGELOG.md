# Changelog

## Unreleased (0.4.0)

### Added

- `degraded: ["<field>:<code>"]` on responses whose best-effort
  enrichment failed (`ROCQ_DEBUG_ENRICHMENT=1` adds detail);
  `enrichment_failures` counters and `lock` contention telemetry in
  `rocq_diag`.

### Changed

- Failure-reason taxonomy single-sourced in `rocq_mcp.taxonomy`
  (wire strings unchanged, pinned by tests).
