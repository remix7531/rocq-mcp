# rocq-mcp concurrency — sharing one server between agents

`rocq-mcp` is **single-tenant per process**. All agent-facing state — the
live `state_id` table, the import cache, the active workspace, and the
single `pet` subprocess — is process-global, and pet calls are globally
serialized (one at a time). Two correctness floors keep concurrent
callers from clobbering each other:

- **LRU-protected state table.** A `state_id` you keep using via
  `from_state` will not be evicted by a peer churning through new states
  (`ROCQ_MAX_STATES`, default 1000).
- **No implicit current state.** `rocq_check` and `rocq_step_multi`
  require `from_state` explicitly — there is no global "last touched"
  state a peer could re-point under you.

The remaining cross-agent costs are latency and RAM: workspace-swap
thrash when peers are on different workspaces, pet RSS growth from
accumulated Fleche cache, and the rare peer `force_restart=True` (which
kills pet under everyone). `rocq_switch` is a second process-global
mutator: it moves the whole server to a different opam switch, kills pet,
clears every caller's state table, and may leave `.vo` artifacts
ABI-incompatible — in shared sessions check `rocq_diag`'s `live_states`
for peer entries before calling it, and prefer pinning the switch at
launch (`opam exec --switch=<name> -- rocq-mcp` in the client config).

## Agent-side recovery: force_restart

When a `state_id` you actively depend on goes missing despite the LRU
floor — typically because a peer just force-restarted pet —
`rocq_start(..., force_restart=True)` kills pet, clears the state table,
respawns fresh, and returns a new `state_id`. "Fresh" is point-in-time: a
concurrent caller can force-restart again right after, so this is
recovery, not enforced isolation. The same call applies to non-contention
triggers (RAM bloat, indexing corruption, a "State N expired" that
repeats after a plain retry). It is NOT needed as routine insurance, and
is unhelpful right after a response already carried `pet_restarted: true`
(pet is already fresh).

## Orchestrator-side monitoring: rocq_diag

When you cannot deploy a separate rocq-mcp per sub-agent, `rocq_diag` is
the monitoring primitive between sub-agent dispatches:

- `live_states[*].file` — entries you did not create signal a foreign
  caller (works when agents are on disjoint files).
- `memory.pet_rss_mb` vs `max_rss_mb_threshold` — accumulated Fleche
  bloat before it forces a restart.
- `recent_errors` — a peer just hit `lock_contended` / `memory_exhausted`
  / a force-restart.
- `lock.contended_total` and `lock.wait_ms_max` — how much callers
  actually park on the single pet lock. Sustained contention here is the
  signal that one shared server is the wrong deployment.
- `load_average["1m"]` vs host CPU count — CPU saturation vs a diverging
  tactic when a timeout fires (`null` on platforms without getloadavg).

## Operator-side hardening: one server per agent

The cleanest deployment is one rocq-mcp subprocess per concurrent agent.
Over stdio this happens naturally when each MCP client launches its own
server; the case needing care is parallel sub-agents within one client,
which inherit the parent's MCP connections and share one rocq-mcp. Prefer
separate top-level invocations over one parent with concurrent
sub-agents.

**Claude Code escape hatch.** Sub-agents in `.claude/agents/<name>.md`
accept an inline `mcpServers` entry in their frontmatter; an inline
definition gives that sub-agent its own rocq-mcp subprocess (own pet, own
state table, own workspace), connected at sub-agent start and torn down
at finish. A string reference instead shares the parent's connection.

```yaml
mcpServers:
  - rocq-mcp:
      type: stdio
      command: rocq-mcp
```

**Worktree per sub-agent.** The escape hatch isolates the pet subprocess;
for filesystem isolation — so concurrent sub-agents can edit `.v` files
without staling each other's sessions — pair it with a `git worktree` per
sub-agent and point `ROCQ_WORKSPACE` at it:

```yaml
mcpServers:
  - rocq-mcp:
      type: stdio
      command: rocq-mcp
      env:
        ROCQ_WORKSPACE: /path/to/worktree-A
```

Each worktree carries its own checkout, its own auto-generated
`_RocqProject`, and its own scratch files — no cross-staling, no
load-path invalidation between agents.

## Timeouts under sharing

Pet-routed tools accept a per-call `timeout=<seconds>` overriding
`ROCQ_PET_TIMEOUT` for that one call (clamped to
`ROCQ_QUERY_TIMEOUT_CAP`, default 300; the response then carries
`clamped_timeout`). On a timeout, prefer bumping `timeout=` per-call over
raising the global default — a large global default lets any one call
park the shared pet lock for that long.
