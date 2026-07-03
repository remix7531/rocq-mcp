# rocq-mcp evals

Regression harness for the MCP tool surface. Two tiers:

- **Tier 1 (scripted, deterministic)** — fixed tool sequences per corpus task,
  driven through an in-memory `fastmcp.Client` against the live server object,
  graded post-hoc by re-running each task's `check` block. Runs as part of the
  normal pytest suite (`tests/test_evals_tier1.py`) whenever `coqc` and `pet`
  are on PATH (they are in CI). Renaming a parameter, changing a failure
  `reason`, or breaking the envelope fails tier 1 immediately.
- **Tier 2 (agent-driven, manual)** — a real agent (`claude -p`) solves the
  same corpus through the MCP server; graded independently after the fact
  (fresh MCP session runs the task's `check` block — the transcript is never
  trusted). Reports success rate, per-tool call histogram, cost, and wall
  time, and diffs against `baselines/baseline.json`. Run locally with
  `uv run python -m evals.runner.run_agent` (needs coqc, pet, the `claude`
  CLI) or via the `Tier-2 evals` workflow dispatch. Write a new baseline
  deliberately with `--baseline` and commit it. Corpus target is ~15 tasks
  (currently 9) — additions welcome, one scenario per task.

## Corpus layout

```
evals/corpus/<task_id>/
  task.yaml     # id, kind (prove|fix|find_lemma), prompt (tier 2), entry_file,
                # budget, and the check block used by BOTH tiers for grading
  files/        # copied into a fresh tmp workspace per run
```

`check` entries are tool calls with expected result subsets. Two conventions:

- `"{workspace}"` in any string arg is replaced with the run's workspace path.
- an arg value of `"@<name>"` is replaced with the content of `<name>` in the
  final workspace (e.g. the proof file the agent edited).

## Running tier 1 locally

```
uv run pytest tests/test_evals_tier1.py -v
```

Tasks skip automatically when `coqc`/`pet` are unavailable.
