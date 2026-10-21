"""
Tests for the specialist agents that write model output to the database.

The shift and supply agents turn a model's JSON into rows. These check that a
proposal is validated before it is written, that a rejected one is reported
rather than swallowed, and that the shift agent plans against the same rules
the gap scan applies.
"""
from __future__ import annotations

import json

import database.crud as crud
from agents import shift_agent, supply_agent
from tests.conftest import groq_response


def test_shift_prompt_states_the_rules_the_scan_uses():
    """It said "2 volunteers per 6-hour block per zone"; the scan uses 4 and has no zones."""
    prompt = shift_agent._SYSTEM_PROMPT
    assert f"per {crud.SHIFT_GAP_BLOCK_HOURS}-hour block" in prompt
    assert f"minimum {crud.SHIFT_GAP_MIN_VOLUNTEERS} volunteers" in prompt
    assert f"{crud.MAX_VOLUNTEER_HOURS_PER_DAY}hr today" in prompt
    assert "6-hour" not in prompt
    assert "zone" not in prompt


async def test_shift_agent_books_only_eligible_volunteers(db, seeded_disaster, fake_groq):
    did = seeded_disaster["disaster_id"]
    real = seeded_disaster["volunteer_id"]
    fake_groq(groq_response(json.dumps({
        "gaps_found": [],
        "gaps_filled": [
            {"volunteer_id": real, "role": "medical",
             "start_time": "2030-01-01T08:00:00+00:00",
             "end_time": "2030-01-01T12:00:00+00:00"},
            # An id the model was never shown.
            {"volunteer_id": 999999, "role": "medical",
             "start_time": "2030-01-01T12:00:00+00:00",
             "end_time": "2030-01-01T16:00:00+00:00"},
            # A backwards time range.
            {"volunteer_id": real, "role": "medical",
             "start_time": "2030-01-01T20:00:00+00:00",
             "end_time": "2030-01-01T16:00:00+00:00"},
            "not even an object",
        ],
        "escalations_needed": [],
        "volunteers_contacted": [],
        "coverage_status": "partial",
    })))

    result = await shift_agent.run_shift_agent(did, db)

    assert len(result["shifts_created_in_db"]) == 1
    assert result["shifts_created_in_db"][0]["volunteer_id"] == real
    reasons = [r["reason"] for r in result["fills_rejected"]]
    assert len(reasons) == 3
    assert any("not among the eligible" in r for r in reasons)
    assert any("end_time must be after" in r for r in reasons)
    assert result["coverage_rules"]["block_hours"] == crud.SHIFT_GAP_BLOCK_HOURS

    shifts = await crud.list_shifts_by_disaster(db, did)
    assert [s.volunteer_id for s in shifts] == [real]


async def test_shift_agent_reports_an_empty_booking_explicitly(db, seeded_disaster, fake_groq):
    """A consumer must not fall back to the model's claimed fills."""
    fake_groq(groq_response(json.dumps({
        "gaps_found": [], "gaps_filled": [], "escalations_needed": [],
        "volunteers_contacted": [], "coverage_status": "ok",
    })))
    result = await shift_agent.run_shift_agent(seeded_disaster["disaster_id"], db)
    assert result["shifts_created_in_db"] == []
    assert result["fills_rejected"] == []


async def test_supply_agent_normalises_priority_and_reports_failures(
    db, seeded_disaster, fake_groq
):
    did = seeded_disaster["disaster_id"]
    fake_groq(groq_response(json.dumps({
        "critical_shortages": [],
        "tasks_created": [
            {"title": "Procure water", "description": "1600 L", "priority": "URGENT!!"},
            "not an object",
        ],
        "supplies_updated": [],
        "reallocation_suggestions": [],
        "overall_supply_status": "critical",
    })))

    result = await supply_agent.run_supply_agent(did, "assess", db)

    assert len(result["tasks_created_in_db"]) == 1
    assert result["tasks_created_in_db"][0]["priority"] == "high"
    assert len(result["tasks_rejected"]) == 1
