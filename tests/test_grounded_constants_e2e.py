"""
End-to-end proof that a system constant the agent quotes is traceable.

The unit tests in ``test_eval_scorers.py`` check the scorer against
hand-written payloads. This file closes the remaining gap: it drives the real
``detect_shift_gaps`` tool through the real orchestrator with a scripted model,
then scores the real answer with the real scorer.

It exists because the failure it guards against was only ever observed by
accident. ``situation_assessment_multi_tool`` flagged an ungrounded ``7.0`` in
one eval run and passed the next — not because anything was fixed, but because
the model happened not to call the coverage tool the second time. Tool choice
belongs to the model, so no LLM-driven run can be relied on to exercise this.
A scripted model can.
"""
from __future__ import annotations

import json

import agents.orchestrator as orchestrator
import database.crud as crud
from evals.scorers import score_grounding
from tests.conftest import FakeLLM


def _claim_text(result) -> str:
    """The parts of an answer the grounding scorer reads (see evals/runner.py)."""
    return "\n".join(
        [str(result.get("orchestrator_summary", ""))]
        + [str(a) for a in result.get("alerts", [])]
    )


def _script(args, summary):
    """
    Call detect_shift_gaps, stop the loop, then answer.

    Three turns, because the orchestrator asks for its structured answer in a
    separate tools-disabled call. Script only two and the scripted answer is
    never reached — the fake model falls back to a stub, and a test meant to
    check the wording passes without ever seeing it.
    """
    return [
        FakeLLM.tool_turn(("detect_shift_gaps", args)),
        FakeLLM.text_turn("done looking"),
        FakeLLM.text_turn(json.dumps({
            "orchestrator_summary": summary,
            "alerts": [],
            "next_recommended_actions": [],
        })),
    ]


async def test_agent_may_state_the_scan_window_it_was_given(
    db, seeded_disaster, fake_llm
):
    did = seeded_disaster["disaster_id"]
    args = json.dumps({"disaster_id": did})

    # One run to learn what the tool actually returned, so the scripted answer
    # quotes real figures rather than assumed ones.
    fake_llm([FakeLLM.tool_turn(("detect_shift_gaps", args))])
    probe = await orchestrator.run_orchestrator(did, "Where are the gaps?", db)
    payload = probe["results"]["detect_shift_gaps"]
    assert payload["window_days"] == crud.SHIFT_GAP_WINDOW_DAYS

    answer = (
        f"No shift coverage for the next {payload['window_days']} days: "
        f"{payload['gap_count']} blocks fall below the staffing floor."
    )

    fake_llm(_script(args, answer))
    result = await orchestrator.run_orchestrator(did, "Where are the gaps?", db)

    assert "detect_shift_gaps" in result["tools_called"]
    assert result["orchestrator_summary"] == answer, "scripted answer never landed"

    grounding = score_grounding(_claim_text(result), result["results"])
    assert grounding["passed"], grounding["detail"]


async def test_agent_may_not_invent_a_different_window(db, seeded_disaster, fake_llm):
    """The window is now visible to the scorer — not exempt from it."""
    did = seeded_disaster["disaster_id"]
    args = json.dumps({"disaster_id": did})
    wrong = crud.SHIFT_GAP_WINDOW_DAYS + 7

    answer = f"No shift coverage for the next {wrong} days."
    fake_llm(_script(args, answer))
    result = await orchestrator.run_orchestrator(did, "Where are the gaps?", db)
    assert result["orchestrator_summary"] == answer, "scripted answer never landed"

    grounding = score_grounding(_claim_text(result), result["results"])
    assert not grounding["passed"], grounding["detail"]
    assert float(wrong) in grounding["unverified"]


async def test_round_limit_notice_is_not_scored_as_an_agent_claim(
    db, seeded_disaster, fake_llm
):
    """
    The orchestrator's own footer used to land in ``alerts``, where the scorer
    read its round count as a numeric claim the agent could not support — and
    the LLM judge independently cited it as the answer's only unsupported
    claim. It belongs to the run, not to the operation.
    """
    did = seeded_disaster["disaster_id"]
    args = json.dumps({"disaster_id": did})

    turns = [FakeLLM.tool_turn(("get_supply_status", args)) for _ in range(20)]
    turns.append(FakeLLM.text_turn(json.dumps({
        "orchestrator_summary": "Reviewed supplies.",
        "alerts": [],
        "next_recommended_actions": [],
    })))
    fake_llm(turns)

    result = await orchestrator.run_orchestrator(did, "Loop forever", db)

    assert any("tool limit" in n for n in result["run_notices"])
    # The notice reaches the operator, but never as one of the agent's claims.
    assert "tool limit" not in _claim_text(result)

    grounding = score_grounding(_claim_text(result), result["results"])
    assert grounding["passed"], grounding["detail"]
