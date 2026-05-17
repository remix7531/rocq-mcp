# Contributing to rocq-mcp

Thanks for working on `rocq-mcp`!  This file documents the
development workflow currently in use.

## Branch model

We use a **per-feature-branch + integration-branch** workflow:

- `main` is always shippable.  Direct pushes to `main` are reserved
  for release commits.
- Each change lives on its own topic branch named with a
  `<kind>/<short-slug>` prefix.  Common kinds:
  - `feat/...` — additive functionality (new tool, new arg, new
    response key).
  - `fix/...` — bug fixes, including envelope-shape corrections.
  - `docs/...` — documentation-only passes (this file landed via
    `docs/comprehensive-pass`).
- When several topic branches are ready to ship together they are
  merged into an **integration branch** (e.g. `integration/audit-fixes`).
  Integration branches sequence merges so envelope-contract tests pass
  at every step and so the `CHANGELOG.md` can be updated coherently.
- Force-pushes are allowed on personal topic branches, **never** on
  `main` or integration branches once they are shared.

## Running tests

```bash
uv pip install -e ".[dev]"
uv run pytest -q
```

The baseline on `main` is the full suite green, with a sizeable skip
count from tests that need `pet` (see below).  Record whatever
`pytest -q` reports on `main` as the baseline for your branch.  Each
in-flight feature branch typically adds tests on top — see the
relevant commit message for the delta.  A docs-only branch should not
move either number.

### Tests that need `pet`

Tests for the pytanque-based tools (`rocq_query`, `rocq_assumptions`,
`rocq_start`, `rocq_check`, `rocq_step_multi`, `rocq_toc`,
`rocq_notations`) require the `pet`
binary from [coq-lsp](https://github.com/ejgallego/coq-lsp) to be on
`$PATH`.  When `pet` is **not** installed, these tests skip
automatically via the `requires_pet` fixture in `conftest.py`, which
accounts for most of the skip count.  CI runs with `pet` installed.

### Envelope contract

`tests/test_envelope_contract.py` locks in the success / failure
envelope shape and the forbidden-key list (notably `verified`).  If
you touch a response shape, run this file first:

```bash
uv run pytest tests/test_envelope_contract.py -q
```

A failure here usually means a tool grew a key that overlaps with a
legacy name, or stopped emitting `success` / `reason` / `error`
correctly.

## Nix flake

A `flake.nix` is provided (currently in-flight on
`feat/nix-flake`).  Once landed:

- `nix build .#rocq-mcp` produces the wheel.
- `nix run .` starts the server with `coqc` and `pet` already on
  `PATH`.

## Commit messages

Use a leading `<kind>(<area>): <subject>` line where possible, e.g.

```
feat(step_multi): per-call timeout= argument
fix(compile_file): warn about .vo rebuilds in active workspaces
docs(README): document response envelope and recovery flows
```

Keep the subject under ~72 characters.  Explain the *why* in the body
when the diff is not self-explanatory.  Co-author trailers are
welcome.
