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


def test_grounding_passes_when_no_numeric_claims():
    result = score_grounding("Supplies look adequate.", SUPPLY_RESULTS)
    assert result["passed"]
    assert result["score"] == 1.0


def test_grounding_tolerates_thousands_separators():
    result = score_grounding("Water needs 2,000 liters.", SUPPLY_RESULTS)
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
