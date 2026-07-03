"""Tier-2 eval runner: a real agent (`claude -p`) solves the corpus.

For each task: materialize a fresh workspace, point a headless Claude
Code session at this repo's rocq-mcp over stdio, let it work, then grade
the FINAL WORKSPACE on a fresh in-memory MCP session (never trust the
transcript). Reports success, tool-call histogram, tokens, cost, and
wall time per task; diffs against the committed baseline when present.

Usage:
    uv run python -m evals.runner.run_agent [--task ID] [--max-turns N]
    uv run python -m evals.runner.run_agent --baseline   # write baseline

Requires: coqc + pet on PATH, the `claude` CLI, and ANTHROPIC_API_KEY
(or an authenticated Claude Code install).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from evals.runner.common import Task, load_corpus, materialize_workspace
from evals.runner.grade import grade

REPO = Path(__file__).resolve().parent.parent.parent
RESULTS_DIR = REPO / "evals" / "results"
BASELINE = REPO / "evals" / "baselines" / "baseline.json"

# Tools the agent may use without prompting.  The bare server prefix
# allowlists every rocq-mcp tool; file tools cover proof assembly.
ALLOWED_TOOLS = "mcp__rocq-mcp,Read,Write,Edit,Glob,Grep"


@dataclass
class TaskResult:
    task_id: str
    kind: str
    passed: bool
    grade_failures: list[str] = field(default_factory=list)
    agent_error: str | None = None
    num_turns: int | None = None
    total_cost_usd: float | None = None
    tool_calls: dict[str, int] = field(default_factory=dict)
    rocq_tool_calls: int = 0
    wall_s: float = 0.0


def _mcp_config(workspace: Path) -> dict[str, Any]:
    return {
        "mcpServers": {
            "rocq-mcp": {
                "command": "uv",
                "args": ["run", "--directory", str(REPO), "rocq-mcp"],
                "env": {"ROCQ_WORKSPACE": str(workspace)},
            }
        }
    }


def _run_claude(
    prompt: str, workspace: Path, cfg_path: Path, max_turns: int
) -> tuple[dict[str, Any] | None, dict[str, int], str | None]:
    """Run headless Claude Code; return (final envelope, tool histogram, error)."""
    cmd = [
        "claude",
        "-p",
        prompt,
        "--mcp-config",
        str(cfg_path),
        "--allowedTools",
        ALLOWED_TOOLS,
        "--max-turns",
        str(max_turns),
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    histogram: dict[str, int] = {}
    envelope: dict[str, Any] | None = None
    try:
        proc = subprocess.run(
            cmd,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=1800,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return None, histogram, f"claude invocation failed: {e!r}"

    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "assistant":
            for block in (event.get("message") or {}).get("content", []) or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = block.get("name", "?")
                    histogram[name] = histogram.get(name, 0) + 1
        elif event.get("type") == "result":
            envelope = event

    if envelope is None:
        tail = (proc.stderr or proc.stdout or "")[-500:]
        return None, histogram, f"no result envelope (exit {proc.returncode}): {tail}"
    return envelope, histogram, None


async def _grade_workspace(task: Task, workspace: Path) -> tuple[bool, list[str]]:
    from fastmcp import Client

    from rocq_mcp.server import mcp

    async with Client(mcp) as client:
        report = await grade(task, workspace, client)
    return report.passed, report.failures


def run_task(task: Task, max_turns: int, keep: bool = False) -> TaskResult:
    result = TaskResult(task_id=task.id, kind=task.kind, passed=False)
    tmp = Path(tempfile.mkdtemp(prefix=f"rocq-eval-{task.id}-"))
    try:
        ws = materialize_workspace(task, tmp / "ws")
        cfg_path = tmp / "mcp.json"
        cfg_path.write_text(json.dumps(_mcp_config(ws)))
        prompt = task.prompt.replace("{workspace}", str(ws))

        started = time.monotonic()
        envelope, histogram, agent_error = _run_claude(prompt, ws, cfg_path, max_turns)
        result.wall_s = round(time.monotonic() - started, 1)
        result.tool_calls = histogram
        result.rocq_tool_calls = sum(
            n for name, n in histogram.items() if name.startswith("mcp__rocq-mcp")
        )
        result.agent_error = agent_error
        if envelope is not None:
            result.num_turns = envelope.get("num_turns")
            result.total_cost_usd = envelope.get("total_cost_usd")

        # Grade regardless of how the agent claims it went.
        result.passed, result.grade_failures = asyncio.run(_grade_workspace(task, ws))
        return result
    finally:
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)


def _diff_against_baseline(results: list[TaskResult]) -> list[str]:
    if not BASELINE.is_file():
        return ["no baseline committed (evals/baselines/baseline.json) — skipped"]
    base = json.loads(BASELINE.read_text())
    base_by_id = {r["task_id"]: r for r in base.get("results", [])}
    notes: list[str] = []
    base_rate = base.get("success_rate")
    rate = sum(r.passed for r in results) / max(1, len(results))
    if base_rate is not None and rate < base_rate:
        notes.append(f"success rate dropped: {base_rate:.0%} -> {rate:.0%}")
    for r in results:
        prev = base_by_id.get(r.task_id)
        if not prev:
            notes.append(f"{r.task_id}: new task (no baseline)")
            continue
        if prev.get("passed") and not r.passed:
            notes.append(f"{r.task_id}: regressed (was passing)")
        prev_calls = prev.get("rocq_tool_calls") or 0
        if prev_calls and r.rocq_tool_calls > prev_calls * 1.25:
            notes.append(
                f"{r.task_id}: tool calls inflated "
                f"{prev_calls} -> {r.rocq_tool_calls}"
            )
    return notes or ["no regressions vs baseline"]


def _render_markdown(results: list[TaskResult], notes: list[str]) -> str:
    lines = [
        "# rocq-mcp tier-2 eval report",
        "",
        f"Tasks: {len(results)} | Passed: {sum(r.passed for r in results)} | "
        f"Success rate: {sum(r.passed for r in results) / max(1, len(results)):.0%}",
        "",
        "| task | kind | passed | rocq calls | turns | cost $ | wall s |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r.task_id} | {r.kind} | {'PASS' if r.passed else 'FAIL'} | "
            f"{r.rocq_tool_calls} | {r.num_turns or '—'} | "
            f"{r.total_cost_usd if r.total_cost_usd is not None else '—'} | "
            f"{r.wall_s} |"
        )
    lines += ["", "## Baseline diff", ""]
    lines += [f"- {n}" for n in notes]
    failures = [r for r in results if not r.passed]
    if failures:
        lines += ["", "## Failures", ""]
        for r in failures:
            lines.append(f"### {r.task_id}")
            if r.agent_error:
                lines.append(f"- agent: {r.agent_error}")
            lines += [f"- {f}" for f in r.grade_failures]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", help="run a single task id")
    parser.add_argument("--max-turns", type=int, default=40)
    parser.add_argument(
        "--baseline",
        action="store_true",
        help="write the run as the new committed baseline",
    )
    parser.add_argument("--keep-workspaces", action="store_true")
    args = parser.parse_args()

    if shutil.which("coqc") is None or shutil.which("pet") is None:
        print("tier-2 evals need coqc and pet on PATH", file=sys.stderr)
        return 2
    if shutil.which("claude") is None:
        print("tier-2 evals need the `claude` CLI", file=sys.stderr)
        return 2

    tasks = load_corpus()
    if args.task:
        tasks = [t for t in tasks if t.id == args.task]
        if not tasks:
            print(f"unknown task {args.task!r}", file=sys.stderr)
            return 2

    results: list[TaskResult] = []
    for task in tasks:
        print(f"== {task.id} ==", flush=True)
        result = run_task(task, args.max_turns, keep=args.keep_workspaces)
        print(
            f"   {'PASS' if result.passed else 'FAIL'} "
            f"({result.rocq_tool_calls} rocq calls, {result.wall_s}s)",
            flush=True,
        )
        results.append(result)

    notes = _diff_against_baseline(results)
    payload = {
        "success_rate": sum(r.passed for r in results) / max(1, len(results)),
        "results": [asdict(r) for r in results],
    }

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (RESULTS_DIR / f"{stamp}.json").write_text(json.dumps(payload, indent=2))
    report_md = _render_markdown(results, notes)
    (RESULTS_DIR / f"{stamp}.md").write_text(report_md)
    print(report_md)

    if args.baseline:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps(payload, indent=2))
        print(f"baseline written to {BASELINE}")

    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
