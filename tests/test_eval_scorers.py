"""
Tests for the evaluation scorers.

An eval harness that cannot fail is worse than no harness — it manufactures
confidence. These tests assert that each scorer rejects the bad behaviour it
exists to detect, not merely that it accepts the good case.
"""
from __future__ import annotations

import pytest

from evals.scorers import (
    score_grounding,
    score_loop_health,
    score_side_effects,
    score_tool_selection,
)

SUPPLY_RESULTS = {
    "get_supply_status": [
        {
            "item_name": "Drinking Water",
            "quantity_needed": 2000.0,
            "quantity_available": 400.0,
            "is_critical": True,
        },
        {
            "item_name": "Baby Formula",
            "quantity_needed": 100.0,
            "quantity_available": 12.0,
            "is_critical": True,
        },
    ]
}


# ---------------------------------------------------------------------------
# Grounding
# ---------------------------------------------------------------------------


def test_grounding_accepts_numbers_present_in_tool_output():
    result = score_grounding("Water: 2000 needed, only 400 available.", SUPPLY_RESULTS)
    assert result["passed"]
    assert result["score"] == 1.0


def test_grounding_accepts_derived_shortfalls():
    """2000 - 400 = 1600 is arithmetic on real data, not a hallucination."""
    result = score_grounding(
        "Water is short by 1600 liters and formula by 88 cans.", SUPPLY_RESULTS
    )
    assert result["passed"], result["detail"]


def test_grounding_rejects_invented_numbers():
    result = score_grounding(
        "We have 7500 liters of water and 42 helicopters deployed.", SUPPLY_RESULTS
    )
    assert not result["passed"]
    assert 7500.0 in result["unverified"]
    assert 42.0 in result["unverified"]


def test_grounding_partial_credit_is_proportional():
    result = score_grounding("Water needs 2000 but we invented 9999.", SUPPLY_RESULTS)
    assert 0.0 < result["score"] < 1.0
    assert result["unverified"] == [9999.0]


def test_grounding_ignores_small_ordinals():
    result = score_grounding("Top 3 issues. Water at 400 of 2000.", SUPPLY_RESULTS)
    assert result["passed"], result["detail"]


def test_grounding_does_not_blindly_excuse_year_like_quantities():
    """A quantity in the 1900-2100 range must still be verified, not skipped."""
    result = score_grounding("We hold 1950 liters of water.", SUPPLY_RESULTS)
    assert not result["passed"]
    assert 1950.0 in result["unverified"]


TASK_RESULTS = {
    "get_overdue_tasks": [
        {
            "id": 1,
            "title": "Clear access road",
            "due_at": "2026-08-30T08:00:00",
            "status": "pending",
        }
    ]
}


def test_grounding_accepts_a_date_restated_in_prose():
    """
    Regression: the hyphens in an ISO-8601 timestamp are not minus signs.

    Parsing "2026-08-30" as 2026, -8, -30 put the wrong sign on every date
    component in the grounded set, so an agent that correctly echoed a due date
    back to the operator was reported as hallucinating it.
    """
    result = score_grounding(
        "Task 1 'Clear access road' was due 30 August at 08:00 and is pending.",
        TASK_RESULTS,
    )
    assert result["passed"], result["detail"]


def test_grounding_still_rejects_an_invented_date():
    result = score_grounding(
        "Task 1 was due 14 July at 19:00.", TASK_RESULTS
    )
    assert not result["passed"], result["detail"]


def test_grounding_still_reads_genuine_negatives_as_negative():
    result = score_grounding("Stock moved by -30 units.", TASK_RESULTS)
    assert not result["passed"], result["detail"]
    assert -30.0 in result["unverified"]


# ---------------------------------------------------------------------------
# Grounding: system constants the agent quotes back
#
# An agent describing a query's own parameters — the window it scanned, the
# threshold that classified an item, the scale a severity is reported on — is
# stating something true. Those numbers used to live only in docstrings, so the
# scorer could not trace them and reported four correct answers as
# hallucinations. The tools now return them, so they are grounded the ordinary
# way. Each case below is paired with the wrong value, to show the scorer got
# no more permissive: it can now see these numbers, not excuse them.
# ---------------------------------------------------------------------------

GAP_RESULTS = {
    "detect_shift_gaps": {
        "window_days": 7,
        "block_hours": 4,
        "min_volunteers": 2,
        "gap_count": 42,
        "gaps": [
            {"date": "2026-09-03", "hour_block": 8, "volunteers_count": 0},
        ],
    }
}

HEALTHY_SUPPLY_RESULTS = {
    "get_supply_status": [
        {
            "item_name": "Blankets",
            "quantity_needed": 100.0,
            "quantity_available": 95.0,
            "is_critical": False,
            "critical_threshold_pct": 30,
        }
    ]
}

STATUS_RESULTS = {
    "get_disaster_status": {
        "id": 1,
        "name": "Storm Watch",
        "severity": 2,
        "severity_scale_max": 5,
        "status": "active",
    }
}


def test_grounding_accepts_the_scan_window_the_tool_reports():
    """
    Regression: "no coverage for the next 7 days" failed
    situation_assessment_multi_tool. The 7 is detect_shift_gaps's own window,
    which the payload now carries.
    """
    result = score_grounding(
        "No shift coverage for the next 7 days: 42 blocks are understaffed.",
        GAP_RESULTS,
    )
    assert result["passed"], result["detail"]


def test_grounding_still_rejects_a_window_the_tool_did_not_report():
    result = score_grounding(
        "No shift coverage for the next 14 days.", GAP_RESULTS
    )
    assert not result["passed"], result["detail"]
    assert 14.0 in result["unverified"]


def test_grounding_accepts_the_critical_threshold_the_tool_applied():
    """Regression: "threshold <30 % availability" failed no_false_shortage_alarm."""
    result = score_grounding(
        "Blankets are at 95 % of the required 100 units. None are flagged "
        "critical (threshold <30 % availability).",
        HEALTHY_SUPPLY_RESULTS,
    )
    assert result["passed"], result["detail"]


def test_grounding_still_rejects_a_threshold_the_tool_did_not_apply():
    result = score_grounding(
        "None are flagged critical (threshold <45 % availability).",
        HEALTHY_SUPPLY_RESULTS,
    )
    assert not result["passed"], result["detail"]
    assert 45.0 in result["unverified"]


def test_grounding_accepts_the_severity_scale_the_tool_reports():
    """Regression: "severity 2/5" failed simple_lookup_is_efficient."""
    result = score_grounding(
        "The disaster remains active with severity 2/5.", STATUS_RESULTS
    )
    assert result["passed"], result["detail"]


def test_grounding_still_rejects_a_severity_scale_the_tool_did_not_report():
    result = score_grounding(
        "The disaster remains active with severity 2/10.", STATUS_RESULTS
    )
    assert not result["passed"], result["detail"]
    assert 10.0 in result["unverified"]


def test_grounding_passes_when_no_numeric_claims():
    result = score_grounding("Supplies look adequate.", SUPPLY_RESULTS)
    assert result["passed"]
    assert result["score"] == 1.0


def test_grounding_tolerates_thousands_separators():
    result = score_grounding("Water needs 2,000 liters.", SUPPLY_RESULTS)
    assert result["passed"], result["detail"]


ID_RESULTS = {
    "get_supply_status": [
        {"id": 3, "disaster_id": 1, "item_name": "Rice",
         "quantity_needed": 50.0, "quantity_available": 40.0},
    ]
}


def test_grounding_takes_no_arithmetic_from_identifiers():
    """
    Regression: ids joined the derived arithmetic, so 50 - 3 = 47 and
    40 + 1 = 41 counted as grounded — meaningless sums that excused invented
    counts. On a real payload this alone accepted about 20 of every 97 small
    integers.
    """
    for invented in ("47", "41", "53", "39"):
        result = score_grounding(f"We have {invented} units of rice.", ID_RESULTS)
        assert not result["passed"], (invented, result["detail"])


def test_grounding_still_accepts_identifiers_and_real_arithmetic():
    result = score_grounding(
        "Supply 3 (Rice) is short by 10 units: 40 of 50, 80% of need.", ID_RESULTS
    )
    assert result["passed"], result["detail"]


# ---------------------------------------------------------------------------
# Tool selection
# ---------------------------------------------------------------------------


def _trace(*tools):
    return [{"step": "tool", "tool": t, "ok": True} for t in tools]


def test_tool_selection_passes_when_required_tool_used():
    result = score_tool_selection(
        _trace("get_supply_status"), required_tools=["get_supply_status"]
    )
    assert result["passed"]


def test_tool_selection_flags_missing_required_tool():
    result = score_tool_selection(
        _trace("get_volunteers"), required_tools=["get_supply_status"]
    )
    assert not result["passed"]
    assert "missing" in result["detail"]


def test_tool_selection_flags_forbidden_write_on_read_only_case():
    result = score_tool_selection(
        _trace("get_supply_status", "create_task"),
        required_tools=["get_supply_status"],
        forbidden_tools=["create_task"],
    )
    assert not result["passed"]
    assert "forbidden" in result["detail"]


def test_tool_selection_any_of_requires_at_least_one():
    assert score_tool_selection(
        _trace("get_volunteers"), any_of_tools=["get_volunteers", "find_available_volunteers"]
    )["passed"]

    assert not score_tool_selection(
        _trace("get_supply_status"),
        any_of_tools=["get_volunteers", "find_available_volunteers"],
    )["passed"]


# ---------------------------------------------------------------------------
# Side effects
# ---------------------------------------------------------------------------


def test_side_effects_detects_write_on_read_only_case():
    result = score_side_effects({"tasks": 0}, {"tasks": 1}, expected_deltas={})
    assert not result["passed"]
    assert "unexpected" in result["detail"]


def test_side_effects_passes_when_expected_write_happened():
    result = score_side_effects(
        {"tasks": 0, "shifts": 0},
        {"tasks": 1, "shifts": 0},
        expected_deltas={"tasks": 1},
    )
    assert result["passed"], result["detail"]


def test_side_effects_flags_missing_expected_write():
    result = score_side_effects({"tasks": 0}, {"tasks": 0}, expected_deltas={"tasks": 1})
    assert not result["passed"]
    assert "expected >= 1" in result["detail"]


def test_side_effects_allows_collateral_writes_when_disabled():
    result = score_side_effects(
        {"tasks": 0, "notes": 0},
        {"tasks": 1, "notes": 3},
        expected_deltas={"tasks": 1},
        forbid_other_writes=False,
    )
    assert result["passed"]


def test_side_effects_permits_an_allowed_extra_write():
    """A discretionary note alongside the expected shift is not a failure."""
    result = score_side_effects(
        {"shifts": 0, "notes": 0},
        {"shifts": 1, "notes": 1},
        expected_deltas={"shifts": 1},
        allowed_extra_writes=["notes"],
    )
    assert result["passed"], result["detail"]


def test_side_effects_allowed_extra_write_is_not_required():
    """Skipping the optional note is equally correct."""
    result = score_side_effects(
        {"shifts": 0, "notes": 0},
        {"shifts": 1, "notes": 0},
        expected_deltas={"shifts": 1},
        allowed_extra_writes=["notes"],
    )
    assert result["passed"], result["detail"]


def test_allowed_extra_writes_does_not_licence_other_tables():
    """Permitting a note must not quietly permit a broadcast."""
    result = score_side_effects(
        {"shifts": 0, "notes": 0, "broadcasts": 0},
        {"shifts": 1, "notes": 1, "broadcasts": 1},
        expected_deltas={"shifts": 1},
        allowed_extra_writes=["notes"],
    )
    assert not result["passed"]
    assert "broadcasts" in result["detail"]
    assert "notes" not in result["detail"]


def test_allowed_extra_writes_still_reports_deletions():
    """`you may add a note` is not `you may delete one`."""
    result = score_side_effects(
        {"shifts": 0, "notes": 2},
        {"shifts": 1, "notes": 1},
        expected_deltas={"shifts": 1},
        allowed_extra_writes=["notes"],
    )
    assert not result["passed"]
    assert "notes: unexpected change of -1" in result["detail"]


# ---------------------------------------------------------------------------
# Loop health
# ---------------------------------------------------------------------------


def test_loop_health_passes_on_clean_run():
    result = score_loop_health(
        {"iterations": 2, "trace": _trace("get_supply_status")}, max_iterations=5
    )
    assert result["passed"], result["detail"]


def test_loop_health_flags_failed_tool_calls():
    result = score_loop_health(
        {
            "iterations": 2,
            "trace": [{"step": "tool", "tool": "x", "ok": False, "error": "boom"}],
        },
        max_iterations=5,
    )
    assert not result["passed"]
    assert "failed tool call" in result["detail"]


def test_loop_health_flags_exceeding_iteration_budget():
    result = score_loop_health(
        {"iterations": 9, "trace": _trace("get_supply_status")}, max_iterations=5
    )
    assert not result["passed"]
    assert "exceeds budget" in result["detail"]


def test_loop_health_flags_a_run_that_called_no_tools():
    result = score_loop_health({"iterations": 1, "trace": []}, max_iterations=5)
    assert not result["passed"]
    assert "no tools" in result["detail"]


# The two runs below are the real traces from eval_report_v5.json. Before
# max_tool_repeats they drew the identical complaint from this scorer — "N
# iterations exceeds budget" — despite being opposite behaviours.


def test_loop_health_flags_the_same_tool_queried_over_and_over():
    """does_not_fabricate_missing_data: 8 calls, 4 distinct, search_notes x5."""
    result = score_loop_health(
        {
            "iterations": 8,
            "trace": _trace(
                "search_notes", "search_notes", "get_supply_status", "list_tasks",
                "get_shifts", "search_notes", "search_notes", "search_notes",
            ),
        },
        max_iterations=4,
    )
    assert not result["passed"]
    assert "redundant calls (search_notes x5)" in result["detail"]


def test_loop_health_does_not_punish_consulting_many_distinct_sources():
    """situation_assessment_multi_tool: 6 sources, no repeats, within budget."""
    result = score_loop_health(
        {
            "iterations": 7,
            "trace": _trace(
                "get_disaster_status", "get_supply_status", "get_shifts",
                "get_volunteers", "get_weather_forecast", "detect_shift_gaps",
            ),
        },
        max_iterations=7,
    )
    assert result["passed"], result["detail"]


def test_loop_health_separates_breadth_from_flailing():
    """The discriminator: same call count, opposite verdicts."""
    breadth = score_loop_health(
        {"iterations": 5, "trace": _trace("a", "b", "c", "d")}, max_iterations=6
    )
    flailing = score_loop_health(
        {"iterations": 5, "trace": _trace("a", "a", "a", "a")}, max_iterations=6
    )
    assert breadth["passed"], breadth["detail"]
    assert not flailing["passed"]
    assert breadth["score"] > flailing["score"]


# ---------------------------------------------------------------------------
# Dataset sanity
# ---------------------------------------------------------------------------


def test_grounding_ignores_forward_looking_recommendations():
    """
    Regression: recommendations are proposals, not claims about the data.

    ``next_recommended_actions`` carries planning horizons ("over the next 24-48
    hours") and suggested quantities ("pre-position an additional 10-15 units").
    Those cannot trace back to tool output by construction, so scoring them for
    grounding failed correct answers. The LLM judge still sees them in full.
    """
    from evals.runner import _answer_text, _claim_text

    result = {
        "orchestrator_summary": "Water is at 400 of 2000 liters.",
        "alerts": ["Drinking Water is critical."],
        "next_recommended_actions": [
            "Monitor consumption over the next 24-48 hours",
            "Pre-position an additional 10-15 units as a buffer",
        ],
    }

    assert score_grounding(_claim_text(result), SUPPLY_RESULTS)["passed"]
    # The full text is still available for keyword expectations.
    assert "24-48 hours" in _answer_text(result)


def test_orchestrator_contract_separates_absence_from_zero():
    """
    Run 6 regression. Asked how many helicopters were deployed against a
    database with no helicopter concept, the agent answered "get_shifts
    returned no entries, so the system records 0 helicopters and 0 boats
    deployed". The judge scored that 0.17 and was right: nothing was ever
    counted, so a count of zero is a fabricated reading.

    The numeric grounding scorer cannot catch this — it scored the same answer
    1.00, because 0 does appear in the tool output. The contract is the only
    place this can be fixed.
    """
    from agents.orchestrator import _FINAL_RESPONSE_INSTRUCTION, _SYSTEM_PROMPT

    combined = _SYSTEM_PROMPT + _FINAL_RESPONSE_INSTRUCTION
    assert "not tracked" in combined
    assert "absence into a measurement" in _SYSTEM_PROMPT
    # And the answer contract must repeat it, since that is where the
    # fabricated frame was actually written.
    assert "count of zero" in _FINAL_RESPONSE_INSTRUCTION


def test_orchestrator_does_not_invite_requerying_an_empty_source():
    """
    The prompt told the agent to "call them again with different arguments if
    the first result is insufficient", and it did — search_notes four times
    over an empty table, burning the full 8-round budget. Empty has to be
    carved out of "insufficient" explicitly.
    """
    from agents.orchestrator import _SYSTEM_PROMPT

    assert "empty is not insufficient" in _SYSTEM_PROMPT
    assert "empty twice" in _SYSTEM_PROMPT


def test_judge_is_shown_the_operation_context():
    """
    Run 6 marked down 8 of 14 cases because the judge saw the operation's own
    name in the answer, could not find it in any tool payload, and called it
    unsupported — "it introduces the name 'Cyclone Vega'". The agent was told
    that name before it called anything.
    """
    from evals.scorers import _build_judge_message

    message = _build_judge_message(
        command="Any shortages?",
        tool_blob='{"supplies": []}',
        answer_blob='{"orchestrator_summary": "Cyclone Vega is stable."}',
        operation_context="Disaster id=1 — 'Cyclone Vega' (cyclone, Coastal Zone, severity 4/5, status active).",
    )
    assert "Cyclone Vega" in message.split("AGENT'S FINAL ANSWER")[0]
    assert "OPERATION CONTEXT" in message


def test_judge_message_omits_the_context_block_when_there_is_none():
    from evals.scorers import _build_judge_message

    message = _build_judge_message(
        command="Any shortages?",
        tool_blob="{}",
        answer_blob="{}",
    )
    assert "OPERATION CONTEXT" not in message
    assert "WHAT A CORRECT ANSWER SHOULD DO" not in message


def test_judge_rubric_permits_restating_the_operation_identity():
    from evals.scorers import _JUDGE_SYSTEM

    assert "operation context" in _JUDGE_SYSTEM.lower()
    assert "NOT an unsupported claim" in _JUDGE_SYSTEM


def test_agent_and_judge_read_the_same_operation_facts():
    """One source, so the briefing and the yardstick cannot drift apart."""
    from types import SimpleNamespace

    from agents.orchestrator import _build_user_message, describe_disaster

    disaster = SimpleNamespace(
        id=1, name="Cyclone Vega", type="cyclone",
        location="Coastal Zone", severity=4, status="active",
    )
    context = describe_disaster(disaster)
    assert context in _build_user_message(disaster, "Any shortages?")
    assert "severity 4/5" in context


def test_constant_bearing_tools_are_required_by_some_case():
    """
    Every tool that returns a constant the agent quotes must be *required* by at
    least one case, not merely allowed by `any_of_tools`.

    Tool choice belongs to the model, so a tool that no case requires may simply
    not get called. That is not hypothetical: the ungrounded `7.0` from
    `detect_shift_gaps` vanished from a run because the agent picked a different
    tool, which read as a fix and was not one, and `severity_scale_max` was
    later confirmed only because a case happened to state a severity. This test
    is the structural guard those two incidents argue for.
    """
    from evals.dataset import CASES

    required = {t for case in CASES for t in case.required_tools}
    for tool in (
        "detect_shift_gaps",       # window_days, block_hours, min_volunteers
        "get_supply_status",       # critical_threshold_pct
        "get_disaster_status",     # severity_scale_max
        "get_weather_forecast",    # the only tool leaving the database
    ):
        assert tool in required, f"no case requires {tool}"


def test_offline_weather_fixture_matches_what_the_case_expects():
    """
    The weather case's expectation quotes the fixture's numbers, so pin them.
    If the fixture ever grows rain, the case would start failing correct answers
    for stating conditions that really are in the payload.
    """
    from mcp_server.weather import _OFFLINE_PAYLOAD

    summary = _OFFLINE_PAYLOAD["summary"]
    assert summary["total_precipitation"] == 0.0
    assert summary["peak_wind_speed"] == 0.0
    assert summary["rain_starts_at"] is None
    assert summary["hours_covered"] == 24


async def test_coverage_case_expectation_matches_the_real_scan(db):
    """
    reports_coverage_gaps_with_its_window demands "42". Pin that to what the
    real tool returns on the real scenario, so a change to the scan cannot
    quietly turn a correct answer into a failure.
    """
    import json

    from agents.tool_registry import execute_tool
    from evals.dataset import CASES_BY_NAME
    from evals.scenarios import SCENARIOS

    case = CASES_BY_NAME["reports_coverage_gaps_with_its_window"]
    truth = await SCENARIOS[case.scenario]()
    record = await execute_tool(
        "detect_shift_gaps", json.dumps({"disaster_id": truth["disaster_id"]})
    )
    payload = json.loads(record["result"])

    assert payload["gap_count"] == 42
    assert "42" in case.must_mention


def test_every_case_points_at_a_real_scenario():
    from evals.dataset import CASES
    from evals.scenarios import SCENARIOS

    for case in CASES:
        assert case.scenario in SCENARIOS, f"{case.name} → unknown {case.scenario}"


def test_every_case_weight_set_sums_to_one():
    from evals.dataset import CASES

    for case in CASES:
        assert sum(case.weights.values()) == pytest.approx(1.0), case.name


async def test_referenced_tools_exist():
    """A typo in a case's tool name would silently make the case unfailable."""
    from agents.tool_registry import get_tool_names
    from evals.dataset import CASES

    known = set(await get_tool_names())
    for case in CASES:
        for tool in (
            list(case.required_tools)
            + list(case.forbidden_tools)
            + list(case.any_of_tools)
        ):
            assert tool in known, f"{case.name} references unknown tool {tool!r}"
