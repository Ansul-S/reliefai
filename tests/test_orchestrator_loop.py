"""
Tests for the orchestrator's tool-calling loop.

The LLM is scripted (see ``FakeLLM`` in conftest) so the loop's control flow is
exercised deterministically and without network access. Tool execution is real:
calls go through the MCP registry to the test database.
"""
from __future__ import annotations

import json

import pytest

from agents import orchestrator
from tests.conftest import FakeLLM

FINAL = json.dumps(
    {
        "orchestrator_summary": "Assessed the situation.",
        "alerts": ["Water is critically low"],
        "next_recommended_actions": ["Order more water"],
    }
)


def _tools_in(trace):
    return [t["tool"] for t in trace if t["step"] == "tool"]


# ---------------------------------------------------------------------------
# LLM -> tool -> result -> final response
# ---------------------------------------------------------------------------


async def test_single_tool_call_then_final_response(db, seeded_disaster, fake_llm):
    did = seeded_disaster["disaster_id"]
    fake_llm(
        [
            FakeLLM.tool_turn(("get_supply_status", json.dumps({"disaster_id": did}))),
            FakeLLM.text_turn("I have what I need."),
            FakeLLM.text_turn(FINAL),
        ]
    )

    result = await orchestrator.run_orchestrator(did, "Check supplies", db)

    assert result["orchestrator_summary"] == "Assessed the situation."
    assert result["tools_called"] == ["get_supply_status"]
    assert _tools_in(result["trace"]) == ["get_supply_status"]
    # The real tool ran against the real database.
    assert any(
        s["item_name"] == "Drinking Water"
        for s in result["results"]["get_supply_status"]
    )


async def test_multiple_sequential_tool_calls(db, seeded_disaster, fake_llm):
    did = seeded_disaster["disaster_id"]
    fake_llm(
        [
            FakeLLM.tool_turn(("get_disaster_status", json.dumps({"disaster_id": did}))),
            FakeLLM.tool_turn(("get_supply_status", json.dumps({"disaster_id": did}))),
            FakeLLM.text_turn("Done."),
            FakeLLM.text_turn(FINAL),
        ]
    )

    result = await orchestrator.run_orchestrator(did, "Full assessment", db)

    assert _tools_in(result["trace"]) == ["get_disaster_status", "get_supply_status"]
    assert result["iterations"] == 3
    assert set(result["results"]) == {"get_disaster_status", "get_supply_status"}


async def test_parallel_tool_calls_in_one_turn(db, seeded_disaster, fake_llm):
    """Several tools requested in a single turn all execute and are all returned."""
    did = seeded_disaster["disaster_id"]
    args = json.dumps({"disaster_id": did})
    fake_llm(
        [
            FakeLLM.tool_turn(
                ("get_disaster_status", args),
                ("get_supply_status", args),
                ("get_volunteers", args),
            ),
            FakeLLM.text_turn("Done."),
            FakeLLM.text_turn(FINAL),
        ]
    )

    result = await orchestrator.run_orchestrator(did, "Assess everything", db)

    assert sorted(_tools_in(result["trace"])) == [
        "get_disaster_status",
        "get_supply_status",
        "get_volunteers",
    ]
    assert result["iterations"] == 2


async def test_repeated_identical_call_is_short_circuited(db, seeded_disaster, fake_llm):
    """The same call with the same arguments is not executed indefinitely."""
    did = seeded_disaster["disaster_id"]
    args = json.dumps({"disaster_id": did})
    fake_llm(
        [
            FakeLLM.tool_turn(("get_supply_status", args)),
            FakeLLM.tool_turn(("get_supply_status", args)),
            FakeLLM.tool_turn(("get_supply_status", args)),
            FakeLLM.text_turn("Done."),
            FakeLLM.text_turn(FINAL),
        ]
    )

    result = await orchestrator.run_orchestrator(did, "Check supplies", db)

    tool_steps = [t for t in result["trace"] if t["step"] == "tool"]
    assert len(tool_steps) == 3
    # The first MAX_DUPLICATE_TOOL_CALLS run; the third is refused.
    assert tool_steps[0]["ok"] is True
    assert tool_steps[1]["ok"] is True
    assert tool_steps[2]["ok"] is False
    assert "Repeated call" in tool_steps[2]["error"]


def _assert_every_tool_call_answered_by_its_own_id(messages):
    """
    The function-calling protocol: an assistant turn's tool_calls must each be
    answered by a following tool message bearing the same id.
    """
    for i, message in enumerate(messages):
        calls = message.get("tool_calls") if message.get("role") == "assistant" else None
        if not calls:
            continue
        answers = messages[i + 1 : i + 1 + len(calls)]
        assert all(a.get("role") == "tool" for a in answers), answers
        assert sorted(a["tool_call_id"] for a in answers) == sorted(
            c["id"] for c in calls
        )


async def test_a_refused_duplicate_still_answers_with_its_call_id(
    db, seeded_disaster, fake_llm
):
    """
    Regression: the refusal record carried no id, so its tool message fell back
    to the tool's *name* as tool_call_id. Groq tolerated that; a stricter
    OpenAI-compatible endpoint rejects an unanswered tool call.
    """
    did = seeded_disaster["disaster_id"]
    args = json.dumps({"disaster_id": did})
    llm = fake_llm(
        [
            FakeLLM.tool_turn(("get_supply_status", args)),
            FakeLLM.tool_turn(("get_supply_status", args)),
            FakeLLM.tool_turn(("get_supply_status", args)),
            FakeLLM.text_turn("Done."),
            FakeLLM.text_turn(FINAL),
        ]
    )

    result = await orchestrator.run_orchestrator(did, "Check supplies", db)

    assert "Repeated call" in result["trace"][-2]["error"]
    _assert_every_tool_call_answered_by_its_own_id(llm.calls[-1]["messages"])


async def test_different_arguments_are_not_treated_as_repeats(
    db, seeded_disaster, fake_llm
):
    did = seeded_disaster["disaster_id"]
    fake_llm(
        [
            FakeLLM.tool_turn(
                ("find_available_volunteers", json.dumps({"disaster_id": did})),
            ),
            FakeLLM.tool_turn(
                (
                    "find_available_volunteers",
                    json.dumps({"disaster_id": did, "required_skills": ["medical"]}),
                ),
            ),
            FakeLLM.text_turn("Done."),
            FakeLLM.text_turn(FINAL),
        ]
    )

    result = await orchestrator.run_orchestrator(did, "Find volunteers", db)

    tool_steps = [t for t in result["trace"] if t["step"] == "tool"]
    assert len(tool_steps) == 2
    assert all(t["ok"] for t in tool_steps)


# ---------------------------------------------------------------------------
# Safety and loop protection
# ---------------------------------------------------------------------------


async def test_max_iteration_limit_is_enforced(
    db, seeded_disaster, fake_llm, monkeypatch
):
    """A model that never stops calling tools is cut off, not left to spin."""
    monkeypatch.setattr(orchestrator, "MAX_TOOL_ITERATIONS", 3)
    monkeypatch.setattr(orchestrator, "MAX_DUPLICATE_TOOL_CALLS", 99)

    did = seeded_disaster["disaster_id"]
    args = json.dumps({"disaster_id": did})
    # Far more tool turns than the limit allows.
    turns = [FakeLLM.tool_turn(("get_supply_status", args)) for _ in range(20)]
    turns.append(FakeLLM.text_turn(FINAL))
    fake_llm(turns)

    result = await orchestrator.run_orchestrator(did, "Loop forever", db)

    assert result["iterations"] == 3
    # The round limit is a remark about the run, so it belongs in run_notices
    # and must not masquerade as an alert about the operation.
    assert any("tool limit" in n for n in result["run_notices"])
    assert not any("tool limit" in a for a in result["alerts"])


async def test_unknown_tool_does_not_abort_the_run(db, seeded_disaster, fake_llm):
    did = seeded_disaster["disaster_id"]
    fake_llm(
        [
            FakeLLM.tool_turn(("no_such_tool", "{}")),
            FakeLLM.tool_turn(
                ("get_supply_status", json.dumps({"disaster_id": did}))
            ),
            FakeLLM.text_turn("Recovered."),
            FakeLLM.text_turn(FINAL),
        ]
    )

    result = await orchestrator.run_orchestrator(did, "Try a bad tool", db)

    tool_steps = [t for t in result["trace"] if t["step"] == "tool"]
    assert tool_steps[0]["ok"] is False
    # The run continued and the model recovered with a real tool.
    assert tool_steps[1]["ok"] is True
    assert result["orchestrator_summary"] == "Assessed the situation."


async def test_malformed_tool_arguments_are_reported_to_the_model(
    db, seeded_disaster, fake_llm
):
    did = seeded_disaster["disaster_id"]
    llm = fake_llm(
        [
            FakeLLM.tool_turn(("get_supply_status", "{not valid json")),
            FakeLLM.text_turn("Done."),
            FakeLLM.text_turn(FINAL),
        ]
    )

    result = await orchestrator.run_orchestrator(did, "Bad args", db)

    tool_steps = [t for t in result["trace"] if t["step"] == "tool"]
    assert tool_steps[0]["ok"] is False
    assert "Invalid arguments" in tool_steps[0]["error"]

    # The error was fed back as a tool message so the model can correct itself.
    tool_messages = [
        m for call in llm.calls for m in call["messages"] if m.get("role") == "tool"
    ]
    assert any("Invalid arguments" in m["content"] for m in tool_messages)


async def test_tool_failure_is_recorded_in_the_log_status(
    db, seeded_disaster, fake_llm
):
    import database.crud as crud

    did = seeded_disaster["disaster_id"]
    fake_llm(
        [
            FakeLLM.tool_turn(("no_such_tool", "{}")),
            FakeLLM.text_turn("Done."),
            FakeLLM.text_turn(FINAL),
        ]
    )

    await orchestrator.run_orchestrator(did, "Bad tool", db)

    logs = await crud.list_logs(db, disaster_id=did, limit=5)
    log = next(l for l in logs if l.agent_name == "orchestrator")
    assert log.status == "error"
    assert log.trace is not None


async def test_llm_failure_returns_controlled_error(db, seeded_disaster, monkeypatch):
    async def explode(*args, **kwargs):
        raise RuntimeError("groq is down")

    monkeypatch.setattr(orchestrator, "_call_groq_with_tools", explode)

    result = await orchestrator.run_orchestrator(
        seeded_disaster["disaster_id"], "Anything", db
    )

    assert "error" in result
    assert "groq is down" in result["error"]


async def test_invalid_final_json_fails_loudly(db, seeded_disaster, fake_llm):
    """Malformed final output must not be returned to callers as valid data."""
    fake_llm(
        [
            FakeLLM.text_turn("thinking done"),
            FakeLLM.text_turn("this is not JSON at all"),
        ]
    )

    result = await orchestrator.run_orchestrator(
        seeded_disaster["disaster_id"], "Anything", db
    )

    assert "error" in result
    assert "not valid JSON" in result["error"]


async def test_final_json_wrapped_in_prose_is_recovered(db, seeded_disaster, fake_llm):
    """The existing recovery path extracts the object from surrounding text."""
    fake_llm(
        [
            FakeLLM.text_turn("done"),
            FakeLLM.text_turn(f"Here is the result:\n```json\n{FINAL}\n```"),
        ]
    )

    result = await orchestrator.run_orchestrator(
        seeded_disaster["disaster_id"], "Anything", db
    )

    assert result["orchestrator_summary"] == "Assessed the situation."


async def test_final_answer_list_fields_are_held_to_lists_of_strings(
    db, seeded_disaster, fake_llm
):
    """A bare string, or an object per alert, is kept as text — not passed through."""
    fake_llm(
        [
            FakeLLM.text_turn("done"),
            FakeLLM.text_turn(json.dumps({
                "orchestrator_summary": "Water is low.",
                "alerts": "Water is critically low",
                "next_recommended_actions": [{"action": "order water", "litres": 1600}],
            })),
        ]
    )

    result = await orchestrator.run_orchestrator(
        seeded_disaster["disaster_id"], "Anything", db
    )

    assert result["alerts"] == ["Water is critically low"]
    assert len(result["next_recommended_actions"]) == 1
    assert isinstance(result["next_recommended_actions"][0], str)
    assert "1600" in result["next_recommended_actions"][0]


async def test_final_answer_without_a_summary_is_an_error(
    db, seeded_disaster, fake_llm
):
    """An empty summary must not come back as a successful run with no answer."""
    fake_llm(
        [
            FakeLLM.text_turn("done"),
            FakeLLM.text_turn(json.dumps({"alerts": ["Water is low"]})),
        ]
    )

    result = await orchestrator.run_orchestrator(
        seeded_disaster["disaster_id"], "Anything", db
    )

    assert "error" in result
    assert "orchestrator_summary" in result["error"]


async def test_tokens_spent_by_a_tool_that_calls_a_model_are_reported(
    db, seeded_disaster, fake_llm, fake_groq
):
    """
    generate_situation_report runs the report agent, a model call of its own.
    The orchestrator's usage used to count only its own turns, so that spend
    appeared nowhere — not in the response, the log row or the cost estimate.
    """
    did = seeded_disaster["disaster_id"]
    fake_llm(
        [
            FakeLLM.tool_turn(
                ("generate_situation_report", json.dumps({"disaster_id": did}))
            ),
            FakeLLM.text_turn("Done."),
            FakeLLM.text_turn(FINAL),
        ]
    )
    # The report agent's own call goes through the real client, scripted below.
    from tests.conftest import groq_response

    fake_groq(groq_response(
        json.dumps({"report_type": "internal", "sections": {},
                    "critical_flags": [], "markdown_report": "# Report"}),
        usage=(100, 50),
    ))

    result = await orchestrator.run_orchestrator(did, "Write a report", db)

    assert result["usage"]["tool_model_tokens"] == 150
    tool_step = next(t for t in result["trace"] if t["step"] == "tool")
    assert tool_step["model_tokens"] == 150
    # The orchestrator's own count is unchanged, so earlier runs stay comparable.
    assert result["usage"]["total_tokens"] == 45


async def test_missing_disaster_returns_error(db, fake_llm):
    fake_llm([FakeLLM.text_turn(FINAL)])
    result = await orchestrator.run_orchestrator(999999, "Anything", db)
    assert "error" in result
    assert "not found" in result["error"]


# ---------------------------------------------------------------------------
# Contract and observability
# ---------------------------------------------------------------------------


async def test_response_preserves_the_existing_api_contract(
    db, seeded_disaster, fake_llm
):
    did = seeded_disaster["disaster_id"]
    fake_llm(
        [
            FakeLLM.tool_turn(("get_supply_status", json.dumps({"disaster_id": did}))),
            FakeLLM.text_turn("Done."),
            FakeLLM.text_turn(FINAL),
        ]
    )

    result = await orchestrator.run_orchestrator(did, "Check supplies", db)

    # Fields the dashboard reads.
    for key in (
        "orchestrator_summary",
        "agents_called",
        "results",
        "alerts",
        "run_notices",
        "next_recommended_actions",
        "execution_time_ms",
    ):
        assert key in result

    assert isinstance(result["agents_called"], list)
    assert isinstance(result["results"], dict)
    assert isinstance(result["alerts"], list)
    assert isinstance(result["run_notices"], list)
    assert isinstance(result["next_recommended_actions"], list)
    assert isinstance(result["execution_time_ms"], int)


async def test_trace_and_usage_are_recorded(db, seeded_disaster, fake_llm):
    did = seeded_disaster["disaster_id"]
    fake_llm(
        [
            FakeLLM.tool_turn(("get_supply_status", json.dumps({"disaster_id": did}))),
            FakeLLM.text_turn("Done."),
            FakeLLM.text_turn(FINAL),
        ]
    )

    result = await orchestrator.run_orchestrator(did, "Check supplies", db)

    steps = [t["step"] for t in result["trace"]]
    assert steps == ["llm", "tool", "llm"]
    assert all("duration_ms" in t for t in result["trace"])

    usage = result["usage"]
    assert usage["total_tokens"] > 0
    assert usage["llm_ms"] >= 0
    assert usage["tool_ms"] >= 0
    assert result["model"]


async def test_log_row_captures_tokens_and_trace(db, seeded_disaster, fake_llm):
    import database.crud as crud

    did = seeded_disaster["disaster_id"]
    fake_llm(
        [
            FakeLLM.tool_turn(("get_supply_status", json.dumps({"disaster_id": did}))),
            FakeLLM.text_turn("Done."),
            FakeLLM.text_turn(FINAL),
        ]
    )

    await orchestrator.run_orchestrator(did, "Check supplies", db)

    logs = await crud.list_logs(db, disaster_id=did, limit=5)
    log = next(l for l in logs if l.agent_name == "orchestrator")
    assert log.status == "success"
    assert log.model
    assert log.total_tokens > 0
    assert isinstance(log.trace, list)
    assert any(step["step"] == "tool" for step in log.trace)


async def test_duplicate_tool_results_get_distinct_keys(db, seeded_disaster, fake_llm):
    """Two different calls to one tool must not overwrite each other."""
    did = seeded_disaster["disaster_id"]
    fake_llm(
        [
            FakeLLM.tool_turn(
                ("find_available_volunteers", json.dumps({"disaster_id": did})),
            ),
            FakeLLM.tool_turn(
                (
                    "find_available_volunteers",
                    json.dumps({"disaster_id": did, "required_skills": ["medical"]}),
                ),
            ),
            FakeLLM.text_turn("Done."),
            FakeLLM.text_turn(FINAL),
        ]
    )

    result = await orchestrator.run_orchestrator(did, "Find volunteers", db)

    assert "find_available_volunteers" in result["results"]
    assert "find_available_volunteers#2" in result["results"]


async def test_tools_are_actually_passed_to_the_model(db, seeded_disaster, fake_llm):
    """Guards against regressing to prompt-only 'tool selection'."""
    llm = fake_llm([FakeLLM.text_turn("done"), FakeLLM.text_turn(FINAL)])

    await orchestrator.run_orchestrator(
        seeded_disaster["disaster_id"], "Anything", db
    )

    first_call = llm.calls[0]
    assert first_call["tools"], "no tool schemas were sent to the model"
    names = {t["function"]["name"] for t in first_call["tools"]}
    assert "get_supply_status" in names
    assert all(t["type"] == "function" for t in first_call["tools"])
