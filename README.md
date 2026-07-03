# rocq-mcp

[![CI](https://img.shields.io/github/actions/workflow/status/LLM4Rocq/rocq-mcp/ci.yml?branch=main&style=for-the-badge)](https://github.com/LLM4Rocq/rocq-mcp/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg?style=for-the-badge)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg?style=for-the-badge)](https://github.com/LLM4Rocq/rocq-mcp/blob/main/LICENSE)

An [MCP](https://modelcontextprotocol.io/) server for [Rocq](https://rocq-prover.org/) (formerly Coq) proof development. It exposes compilation, verification, querying, and interactive tactic stepping as MCP tools, so that LLM agents can write and check Rocq proofs.

- **Thirteen MCP tools** backed by [pet](https://github.com/ejgallego/coq-lsp) (Rocq's coq-lsp interactive backend).
- **Interactive session** that keeps imports warm across calls — inspect goals, search the environment, step through tactics without re-paying coqc's import cost.
- **Staged verification.** Sandboxed audit of admits, axioms, and statement mismatches.
- **Agent-first surface.** Server instructions, tool annotations, on-demand documentation resources, and workflow prompts.

## Tools

<!-- BEGIN GENERATED: tools (scripts/gen_docs.py) -->
| Tool | What it does |
|------|--------------|
| **`rocq_compile`** | Compile Rocq source from a string buffer via coqc. |
| **`rocq_compile_file`** | Compile a .v file on disk via coqc — whole-file check and final verification. |
| **`rocq_verify`** | Verify a proof proves the original statement — sandboxed admit/axiom/statement check. |
| **`rocq_query`** | Run a Rocq query (Search / Check / Print / About / Locate) and return its output. |
| **`rocq_assumptions`** | List the axioms a theorem depends on (Print Assumptions), parsed. |
| **`rocq_toc`** | Outline a .v file: definitions, lemmas, theorems, and sections as a hierarchy. |
| **`rocq_notations`** | Resolve every notation in a statement: which notation, scope, and module. |
| **`rocq_start`** | Open an interactive proof session; returns a state_id plus the goals there. |
| **`rocq_step_multi`** | Try up to 20 tactics against one state; report each outcome without committing. |
| **`rocq_check`** | Run proof commands from a held state; returns a new state_id — the commit step. |
| **`rocq_diag`** | Server runtime diagnostics: pet health, memory, lock contention, recent errors. |
| **`rocq_health`** | Toolchain health: is coqc resolvable, and which opam switch is this server on? |
| **`rocq_switch`** | Switch the running server to another opam switch — process-global and destructive. |
<!-- END GENERATED: tools -->

## Agent documentation

This README covers installing and operating the server. The **agent-facing
documentation ships inside the server itself**:

- **Server instructions** (auto-loaded into the model's context): the core
  proof loop, the explicit-`from_state` rule, the failure envelope and its
  `reason` taxonomy, timeout semantics.
- **Guides** (MCP resources, fetched on demand; `@`-mentionable in Claude
  Code):
  - `rocq://guide/workflows` — choosing a tool, proof patterns, query
    import/scope rules, scratch iteration, position semantics, sub-agent
    briefing.
  - `rocq://guide/failures` — the recovery playbook for every failure
    `reason`, `state_capture_status`, typo recovery, the `pet_restarted`
    diagnostics playbook.
  - `rocq://guide/concurrency` — sharing one server between agents,
    per-sub-agent isolation (own server + git worktree), `rocq_switch`
    caveats.
  - `rocq://guide/responses` — field-level response reference, truncation
    caps, size-control parameters.
- **Prompts** (slash commands in Claude Code): `prove_theorem(file,
  theorem)` and `debug_compile_error(file)` package the recommended
  workflows.

Every failure response carries `{success: false, error, reason}` with a
fixed 11-value `reason` taxonomy — agents dispatch on `reason`, never on
message text: `validation`, `not_found`, `timeout`, `crashed`,
`memory_exhausted`, `lock_contended`, `unavailable`, `tactic_failed`,
`compile_error`, `axiom_dependency`, `type_mismatch`.

## Prerequisites

- **Rocq / Coq** — `coqc` must be on your `PATH`. If the workspace contains
  a `_RocqProject` or `_CoqProject` file, the server parses it for
  load-path flags (`-Q`, `-R`, `-I`). For **dune projects** the server
  auto-detects load paths via `dune coq top` (once per `(coq.theory ...)`
  stanza) and writes a `_RocqProject` in the workspace so coq-lsp picks
  them up too — add that generated file to `.gitignore`. Otherwise it
  defaults to `-Q <workspace> Test`.
- **pet** (from [coq-lsp](https://github.com/ejgallego/coq-lsp)) —
  **recommended**. Powers the interactive tools and the proof-state
  enrichment / multi-error walker on `rocq_compile_file`. Without `pet`
  you fall back to coqc-only operation (`rocq_compile`,
  `rocq_compile_file` with first error only, `rocq_verify`, `rocq_diag`,
  `rocq_health`) — a substantial reduction in what an agent can do.
- **Python 3.11+**

## Installation

Using [uv](https://docs.astral.sh/uv/):

```bash
# Install (includes pytanque for interactive tools)
uv pip install -e .

# For development (pytest, ruff, black, ...)
uv pip install -e ".[dev]"
```

Or run straight from git without cloning:

```bash
uvx --from git+https://github.com/LLM4Rocq/rocq-mcp rocq-mcp
```

## Running

The server uses stdio transport:

```bash
rocq-mcp
```

### MCP client configuration

Add to your MCP client configuration (e.g. Claude Code `.mcp.json`,
Claude Desktop):

```json
{
  "mcpServers": {
    "rocq-mcp": {
      "command": "rocq-mcp",
      "env": {
        "ROCQ_WORKSPACE": "/path/to/your/rocq/project"
      }
    }
  }
}
```

> **Switch selection:** the server resolves `coqc` and `pet` from the
> environment of **the process your MCP client launched** — not from your
> interactive shell — so the opam switch is fixed at server-launch time.
> Pin it explicitly, e.g. register the command as
> `opam exec --switch=<name> -- rocq-mcp`, or set `env` (PATH /
> OPAM_SWITCH_PREFIX) in the client config. Call `rocq_health` to see the
> switch the server actually resolves; `rocq_switch` can change it
> in-session but discards all live proof states (see
> `rocq://guide/concurrency`).

> **Multi-agent deployments:** the cleanest setup is one `rocq-mcp`
> subprocess per concurrent agent — see `rocq://guide/concurrency` for
> the Claude Code sub-agent `mcpServers` pattern and the
> worktree-per-agent recipe.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ROCQ_WORKSPACE` | current directory | Working directory for Rocq compilation; the final fallback when no project marker is found by walking up from the file. When set explicitly, all workspace parameters are constrained to this directory or its subdirectories. |
| `ROCQ_COQC_TIMEOUT` | `60` | Timeout (seconds) for `rocq_compile` / `rocq_compile_file` |
| `ROCQ_VERIFY_TIMEOUT` | `120` | Timeout (seconds) for `rocq_verify` |
| `ROCQ_PET_TIMEOUT` | `30` | Timeout (seconds) for pytanque-based tools |
| `ROCQ_PET_TIMEOUT_GRACE` | `10` | Extra process-level grace (seconds) on top of Rocq-level soft timeouts |
| `ROCQ_QUERY_TIMEOUT_CAP` | `300` | Cap (seconds) on the per-call `timeout` parameter of any pytanque-based tool; larger values are clamped and the response carries `clamped_timeout` |
| `ROCQ_ENRICHMENT_TIMEOUT_CAP` | `5.0` | Cap (seconds) on per-call proof-state capture after a compile failure |
| `ROCQ_COMPILE_MULTI_ERROR_CAP` | `20` | Max per-declaration errors collected on a failed `rocq_compile_file` (`0` disables) |
| `ROCQ_COMPILE_MULTI_ERROR_TIMEOUT` | `5.0` | Per-chunk timeout (seconds) for the multi-error walker |
| `ROCQ_MAX_PET_RSS_MB` | `min(50% of RAM, 16384)` | Max pet subprocess RSS (MB); on breach the call aborts with `reason: "memory_exhausted"` and `pet_restarted: true` |
| `ROCQ_MAX_STATES` | `1000` | Cap on the in-memory state table (LRU-evicted). Bump when multiple callers share one process |
| `ROCQ_COQC_BINARY` | `coqc` | Path to the `coqc` binary |
| `ROCQ_MAX_SOURCE_SIZE` | `1000000` | Maximum source size in bytes |
| `ROCQ_DEBUG_ENRICHMENT` | unset | Set to `1` to include exception text (`degraded_detail`) alongside the `degraded` response field |

## Security Model

The verification tool (`rocq_verify`) uses defense in depth with three verification phases and multiple security layers.

### Verification phases

`rocq_verify` tries up to three phases in sequence, falling back to the next if the previous one times out:

1. **Phase 1 — Module M sandbox.** The proof is wrapped inside `Module M. ... End M.`. The theorem is re-stated outside and proved via `exact M.<name>`. This is the strongest sandbox but can time out on compute-heavy proofs.
2. **Phase 2 — Shared-defs template.** For problems with Inductive/Record/Definition types, type definitions are placed outside Module M to avoid nominal typing mismatches, while the proof stays inside the sandbox.
3. **Phase 3 — Direct verification.** When Phase 1/2 times out or fails, the proof is compiled standalone and correctness is verified by comparing `Check <name>.` output against the problem statement's expected type after normalization. Additional security checks compensate for the lack of a sandbox.

### Layer 1: Module M sandbox (Phases 1 & 2)

Prevents **type redefinition cheating** (Inductive/Record types are generative, so redefining `nat` as `bool` inside Module M cannot unify with the real `nat` outside), **axiom spoofing** (user axioms get an `M.` prefix in `Print Assumptions`, which the whitelist rejects), **`Admitted`/`Abort`** (caught by `Print Assumptions`), and **module escape** (`End M.`, `Reset`/`Back`/`Undo` are forbidden commands).

### Layer 2: Forbidden command scanning

Source is scanned for dangerous commands **after stripping comments** (the scanner matches Rocq's lexer exactly, including string literals inside comments, preventing desynchronization attacks like `(* " (* " *) End M.`):

| Category | Commands |
|----------|----------|
| Filesystem | `Redirect`, `Extraction "..."`, `Separate Extraction`, `Recursive Extraction`, `Extraction Library`, `Cd`, `Load` |
| Code loading | `Declare ML Module`, `Add LoadPath`, `Add Rec LoadPath`, `Add ML Path` |
| Sandbox escape | `End M.`, `Reset`, `Back`, `Undo` |
| Safety bypass | `bypass_check`, `Unset Guard Checking`, `Unset Positivity Checking`, `Unset Universe Checking` |
| Escape hatches | `Drop` (OCaml toplevel) |

### Layer 3: Print Assumptions axiom whitelist

`Print Assumptions` output is checked against a whitelist of standard-library axioms (classical logic, functional extensionality, Reals axioms, primitive int/float/array/string operations, mathcomp.classical re-exports, ...). Qualified axioms must carry a recognized stdlib prefix; bare module-name prefixes are intentionally **not** trusted, so a user `Axiom EM : False.` cannot be auto-trusted by mimicking mathcomp's short form.

### Phase 3 security checks

Without the Module M sandbox, Phase 3 additionally rejects `Admitted`/`admit`/`give_up` outright, blocks `Axiom`/`Parameter`/`Conjecture` declarations, runs the same forbidden-command scan and axiom whitelist, and compares normalized types. Known limitations: type redefinition attacks, notation/scope redefinition before identically-texted definitions, and stdlib function shadowing are not caught in this phase. The `verification_method` response field reports which phase produced the verdict (`module_m`, `shared_defs`, `direct`).

### Trusted anchor

The `problem_statement` parameter is a **trusted anchor**: the server verifies the proof proves that statement, not that the statement is the right problem. Callers must source `problem_statement` from a trusted place (e.g. a file on disk), not from the LLM being evaluated.

### Path validation

All tools that accept file paths validate that resolved paths stay within the configured workspace (preventing path traversal). Project files (`_RocqProject`/`_CoqProject`) are parsed with an `-arg` allowlist (dangerous flags like `-load-vernac-source` are dropped) and path containment checks.

## Running Tests

```bash
uv run pytest
```

Tests for pytanque-based tools require `pet`; integration tests skip
automatically when `coqc`/`pet` are unavailable (CI installs both). The
eval harness under `evals/` runs the same way — see `evals/README.md`.

## Project Structure

```
src/rocq_mcp/
  server.py              MCP app: instructions, 13 tool wrappers, resources,
                         prompts, pet subprocess management
  taxonomy.py            The failure-reason taxonomy (wire protocol)
  envelope.py            Failure envelope + degraded-enrichment reporting
  compile.py             coqc-based tools: compile, compile_file, verify
  compile_enrichment.py  Compile-error proof-state capture + multi-error walk
  interactive.py         pytanque-based tools: start, check, step_multi,
                         query, assumptions, toc, notations
  verify.py              Rocq lexer scanner, Module M verification,
                         Print Assumptions parsing
  proof_walk.py          Whole-file multi-error walker
  diag.py                rocq_diag snapshot builder
  health.py              rocq_health / rocq_switch backing
  guides/                Agent documentation served as MCP resources
evals/                   Eval harness (see evals/README.md)
scripts/gen_docs.py      Regenerates the README tools table from the registry
tests/                   Test suite
```

## License

Apache 2.0 — see [LICENSE](LICENSE) for details.
