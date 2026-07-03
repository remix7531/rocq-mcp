"""Tier-1 scripted scenarios — one fixed tool sequence per corpus task.

Each scenario drives the intended workflow through a connected
``fastmcp.Client`` and leaves the workspace in the final state the task's
``check`` block will grade. Scenarios assert intermediate envelope shape
(that is the point: a renamed field or changed reason string fails here),
but final pass/fail is always decided by ``grade.grade`` on a fresh session.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from evals.runner.common import result_dict

ADD_0_R_TACTICS = (
    "intros n. induction n as [| n' IH]. "
    "- reflexivity. - simpl. rewrite IH. reflexivity."
)

ADD_0_R_PROOF = """\
Proof.
  intros n. induction n as [| n' IH].
  - reflexivity.
  - simpl. rewrite IH. reflexivity.
Qed.
"""


async def _call(client: Any, tool: str, **args: Any) -> dict[str, Any]:
    return result_dict(await client.call_tool(tool, args))


def _finish_file(path: Path, proof_block: str) -> None:
    """Replace the trailing ``Admitted.`` with a real proof block."""
    text = path.read_text()
    assert "Admitted." in text, f"expected Admitted. in {path}"
    path.write_text(text.replace("Admitted.", proof_block.rstrip() + "\n"))


async def prove_add_0_r(client: Any, ws: Path) -> None:
    file = str(ws / "Add0r.v")
    start = await _call(
        client, "rocq_start", file=file, theorem="add_0_r", workspace=str(ws)
    )
    assert start["success"] is True, start
    assert isinstance(start["state_id"], int), start

    chk = await _call(
        client, "rocq_check", from_state=start["state_id"], body=ADD_0_R_TACTICS
    )
    assert chk["success"] is True, chk
    assert chk["proof_finished"] is True, chk

    _finish_file(ws / "Add0r.v", ADD_0_R_PROOF)
    comp = await _call(client, "rocq_compile_file", file=file, workspace=str(ws))
    assert comp["success"] is True, comp


async def prove_andb_comm(client: Any, ws: Path) -> None:
    file = str(ws / "AndbComm.v")
    start = await _call(
        client, "rocq_start", file=file, theorem="andb_comm_ev", workspace=str(ws)
    )
    assert start["success"] is True, start

    chk = await _call(
        client,
        "rocq_check",
        from_state=start["state_id"],
        body="intros a b. destruct a; destruct b; reflexivity.",
    )
    assert chk["success"] is True, chk
    assert chk["proof_finished"] is True, chk

    _finish_file(
        ws / "AndbComm.v",
        "Proof.\n  intros a b. destruct a; destruct b; reflexivity.\nQed.\n",
    )
    comp = await _call(client, "rocq_compile_file", file=file, workspace=str(ws))
    assert comp["success"] is True, comp


async def fix_wrong_tactic(client: Any, ws: Path) -> None:
    file = str(ws / "Broken.v")

    comp = await _call(client, "rocq_compile_file", file=file, workspace=str(ws))
    assert comp["success"] is False, comp
    assert comp["reason"] == "compile_error", comp
    assert "error_positions" in comp, comp
    # pet is available in this tier, so enrichment must at least report
    # whether it captured a proof state at the error.
    assert "state_capture_status" in comp, comp

    start = await _call(
        client,
        "rocq_start",
        file=file,
        theorem="add_0_r_broken",
        workspace=str(ws),
    )
    assert start["success"] is True, start

    chk = await _call(
        client, "rocq_check", from_state=start["state_id"], body=ADD_0_R_TACTICS
    )
    assert chk["success"] is True, chk
    assert chk["proof_finished"] is True, chk

    text = (ws / "Broken.v").read_text()
    text = text.replace(
        "Proof.\n  intros n. reflexivity.\nQed.",
        ADD_0_R_PROOF.rstrip(),
    )
    (ws / "Broken.v").write_text(text)
    comp = await _call(client, "rocq_compile_file", file=file, workspace=str(ws))
    assert comp["success"] is True, comp


async def find_lemma_add_comm(client: Any, ws: Path) -> None:
    q = await _call(
        client,
        "rocq_query",
        preamble="From Coq Require Import Arith.",
        command="Search (_ + _ = _ + _).",
    )
    assert q["success"] is True, q
    assert "add_comm" in str(q.get("output", "")), q

    s = await _call(
        client,
        "rocq_search",
        preamble="From Coq Require Import Arith.",
        pattern="(_ + _ = _ + _)",
    )
    assert s["success"] is True, s
    names = [h.get("name", "") for h in s["hits"]]
    assert any("add_comm" in n for n in names), s


async def prove_with_step_multi(client: Any, ws: Path) -> None:
    file = str(ws / "AddComm.v")
    start = await _call(
        client, "rocq_start", file=file, theorem="my_add_comm", workspace=str(ws)
    )
    assert start["success"] is True, start

    sm = await _call(
        client,
        "rocq_step_multi",
        tactics=["reflexivity.", "auto.", "lia."],
        from_state=start["state_id"],
    )
    assert sm["success"] is True, sm
    winners = [
        r["tactic"]
        for r in sm["results"]
        if r.get("success") and r.get("proof_finished")
    ]
    assert "lia." in winners, sm

    chk = await _call(client, "rocq_check", from_state=start["state_id"], body="lia.")
    assert chk["success"] is True, chk
    assert chk["proof_finished"] is True, chk

    _finish_file(ws / "AddComm.v", "Proof.\n  lia.\nQed.\n")
    comp = await _call(client, "rocq_compile_file", file=file, workspace=str(ws))
    assert comp["success"] is True, comp


async def prove_negb_involutive(client: Any, ws: Path) -> None:
    file = str(ws / "Negb.v")
    start = await _call(
        client, "rocq_start", file=file, theorem="negb_inv", workspace=str(ws)
    )
    assert start["success"] is True, start
    chk = await _call(
        client,
        "rocq_check",
        from_state=start["state_id"],
        body="intros b. destruct b; reflexivity.",
    )
    assert chk["success"] is True and chk["proof_finished"] is True, chk
    # The server now assembles the script — use it directly.
    assert "proof_script" in chk, chk
    (ws / "Negb.v").write_text(chk["proof_script"])
    comp = await _call(client, "rocq_compile_file", file=file, workspace=str(ws))
    assert comp["success"] is True, comp


async def prove_app_nil_r(client: Any, ws: Path) -> None:
    file = str(ws / "AppNil.v")
    start = await _call(
        client, "rocq_start", file=file, theorem="app_nil_right", workspace=str(ws)
    )
    assert start["success"] is True, start
    chk = await _call(
        client,
        "rocq_check",
        from_state=start["state_id"],
        body=(
            "intros A l. induction l as [| x xs IH]. "
            "- reflexivity. - simpl. rewrite IH. reflexivity."
        ),
        goals_format="diff",
    )
    assert chk["success"] is True and chk["proof_finished"] is True, chk
    _finish_file(
        ws / "AppNil.v",
        "Proof.\n  intros A l. induction l as [| x xs IH].\n"
        "  - reflexivity.\n  - simpl. rewrite IH. reflexivity.\nQed.\n",
    )
    comp = await _call(client, "rocq_compile_file", file=file, workspace=str(ws))
    assert comp["success"] is True, comp


async def fix_missing_import(client: Any, ws: Path) -> None:
    file = str(ws / "NeedsLia.v")
    comp = await _call(client, "rocq_compile_file", file=file, workspace=str(ws))
    assert comp["success"] is False, comp
    assert comp["reason"] == "compile_error", comp

    text = (ws / "NeedsLia.v").read_text()
    (ws / "NeedsLia.v").write_text("From Coq Require Import Lia.\n\n" + text)
    comp = await _call(client, "rocq_compile_file", file=file, workspace=str(ws))
    assert comp["success"] is True, comp


async def find_lemma_mul_comm(client: Any, ws: Path) -> None:
    s = await _call(
        client,
        "rocq_search",
        preamble="From Coq Require Import Arith.",
        pattern="(_ * _ = _ * _)",
    )
    assert s["success"] is True, s
    names = [h.get("name", "") for h in s["hits"]]
    assert any("mul_comm" in n for n in names), s


SCENARIOS = {
    "prove_add_0_r": prove_add_0_r,
    "prove_andb_comm": prove_andb_comm,
    "fix_wrong_tactic": fix_wrong_tactic,
    "find_lemma_add_comm": find_lemma_add_comm,
    "prove_with_step_multi": prove_with_step_multi,
    "prove_negb_involutive": prove_negb_involutive,
    "prove_app_nil_r": prove_app_nil_r,
    "fix_missing_import": fix_missing_import,
    "find_lemma_mul_comm": find_lemma_mul_comm,
}
