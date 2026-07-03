# Contributing

## Dev workflow

```bash
uv sync --extra dev          # deps (pytest, ruff, black, mypy, pyyaml)
uv run pytest                # unit subset without coqc/pet; full suite with them
uv run ruff check src tests evals scripts
uv run black src tests evals scripts
uv run mypy                  # strict on leaf modules, baseline elsewhere
uv run python scripts/gen_docs.py   # regenerate the README tools table
```

CI runs the full suite against a real `rocq-prover` + `coq-lsp` toolchain
(Python 3.11 and 3.14), a toolchain-free unit job (3.12/3.13), ruff, black,
mypy, and the README-generation check. The tier-1 evals
(`tests/test_evals_tier1.py`) run wherever `coqc`+`pet` exist; tier-2
agent-driven evals are a manual workflow dispatch (see `evals/README.md`).

### Invariants that gate every change

- `tests/test_envelope_contract.py` — every failure response is
  `{success: false, error, reason}`; do not add required failure keys.
- `tests/test_taxonomy.py` — the `reason` strings are wire protocol.
  Adding one means updating the taxonomy enum, the server instructions,
  the README, the failures guide, and the pinned tests together (the
  docs-sync tests will point at anything you miss).
- `tests/test_mcp_surface.py` — tool inventory, annotations, description
  budgets (≤2,000 chars each, ≤1,200 avg), resources, prompts.
- New response fields that are best-effort must use the
  `degraded`-notes convention (`rocq_mcp/envelope.py`), not silent
  `None`/`[]`.

### The ruff/F401 trap

Several names are bound on `rocq_mcp.server` purely so submodules and
tests can read them as `_server.<NAME>` (and monkeypatch them there).
They look unused to static analysis — they carry per-name
`# noqa: F401` markers. Do not "clean them up"; removing one severs the
attribute path at runtime.

## Fork and upstreaming policy

This repository (remix7531/rocq-mcp) is a fork of
[LLM4Rocq/rocq-mcp](https://github.com/LLM4Rocq/rocq-mcp). Wire behavior
(the JSON payloads, the failure envelope, the reason taxonomy) is kept
identical-or-additive so cherry-picks stay cheap in both directions.

Recommended upstreaming order (small, behavior-preserving first):

1. `taxonomy.py` / `envelope.py` / `config.py` extractions + the
   degraded-notes convention (pure refactors with tests).
2. Server instructions + tool annotations + description diet + the
   guide resources/prompts (protocol-surface only).
3. Response slimming (step_multi dedup, `goals_format`, `include_raw`)
   — carries two documented default changes (see CHANGELOG).
4. The domain wave (`rocq_search`, `rocq_goal`, `proof_script`,
   step_multi upgrades) — note `rocq_goal` supersedes upstream's
   `add-rocq-goal` branch and the step_multi timing work supersedes
   `feat/step-multi-per-tactic-timeouts-and-timing`.

## Deferred: the server.py decomposition

`server.py` still mixes three concerns (tool wrappers, pet runtime,
workspace resolution) with deliberate bottom-of-file imports resolving
the cycle. The target layout is:

```
workspace.py     path validation, dune/_CoqProject parsing, artifact cleanup
pet_runtime.py   locks, watchdog, _run_with_pet, invalidation hooks
app.py           FastMCP instance + instructions + lifespan
tools.py         the tool wrappers + envelope finalizers
server.py        façade: re-exports what tests import + main()
```

It is **gated on first landing or closing the outstanding fork feature
branches** (`feat/file-anchored-tools`, `feat/bullet-focus-payload`,
`feat/per-call-timeout-extended`, `feat/nix-flake`, ...) — the move
touches ~115 test monkeypatch sites and would make those branches
unrebasable. When executing it: move code verbatim (no logic edits in
the same PR), keep attribute-style access for mutable globals (the pet
lock object gets *replaced* at runtime — late binding is load-bearing),
and never re-export moved patch-targets from the façade so stale
monkeypatches fail loudly. Afterwards, enable ruff `TID251` to ban
`rocq_mcp.server` imports inside `src/` so the cycle cannot regrow, and
extend the mypy strict list.

## Other tracked follow-ups

- **PyPI publishing** is blocked by the `pytanque @ git+...` direct
  reference in `pyproject.toml` (PyPI rejects direct URL deps). The
  unblock is upstream publishing pytanque to PyPI — file/track an issue
  on LLM4Rocq/pytanque; a trusted-publisher `release.yml` is trivial
  once that lands. Do not vendor pytanque.
- **Nix flake**: `feat/nix-flake` exists but pins pytanque via an
  absolute local path (unusable for anyone else). Fix by building
  pytanque in-flake via `buildPythonPackage` + `fetchFromGitHub` at the
  v0.2.2 tag, then add a `nix flake check` CI job gated on
  `hashFiles('flake.*')`.
- **MCPB bundling**: rejected — the server is useless without a
  multi-GB opam toolchain that a bundle cannot ship, and the audience
  runs Claude Code with `.mcp.json` where `uvx --from git+...` works
  today.
- **Eval corpus**: target ~15 tasks (currently 9); every task needs a
  tier-1 scenario (`tests/test_evals_meta.py` enforces parity).
- **pytanque upstream asks** that would unlock further improvements:
  bind `petanque/proof_info` (robust statement recovery for
  `proof_script`), `petanque/state/proof/hash` (principled step_multi
  dedup), and server-side `premises` filtering (a real
  premise-selection tool).
- **v0.5 candidates**: `rocq_inspect` (typed About/Print/Check/Locate,
  absorbing `rocq_notations` to keep the tool count at 15) and a
  separate destructive-annotated `rocq_restart` replacing the
  `force_restart` boolean on `rocq_start`.
