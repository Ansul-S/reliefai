"""
The golden dataset.

Cases are chosen to catch the failure modes that actually matter for a
tool-using agent, not just to demonstrate the happy path:

  * does it call the right tools (and refrain from the wrong ones)
  * does it read real data instead of inventing numbers
  * does it write to the database only when the command asks for a change
  * does it stay quiet when the data shows no problem
  * does it admit ignorance instead of fabricating

``expected_deltas`` counts rows created per table. ``forbid_other_writes``
defaults to True, so any case that does not list a table asserts that table was
left untouched.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence


@dataclass(frozen=True)
class EvalCase:
    name: str
    scenario: str
    command: str
    tags: Sequence[str] = ()

    # Tool selection
    required_tools: Sequence[str] = ()
    forbidden_tools: Sequence[str] = ()
    any_of_tools: Sequence[str] = ()

    # Side effects (row-count deltas per table)
    expected_deltas: Optional[Dict[str, int]] = None
    forbid_other_writes: bool = True

    # Loop health
    max_iterations: int = 5

    # Judge guidance
    expectation: str = ""

    # Substrings that must / must not appear in the final answer (lowercased)
    must_mention: Sequence[str] = ()
    must_not_mention: Sequence[str] = ()

    weights: Dict[str, float] = field(
        default_factory=lambda: {
            "tool_selection": 0.25,
            "grounding": 0.25,
            "side_effects": 0.20,
            "loop_health": 0.10,
            "judge": 0.20,
        }
    )


READ_ONLY_TOOLS_FORBIDDEN = (
    "add_note",
    "create_task",
    "schedule_shift",
    "cancel_shift",
    "update_supply",
    "update_task",
    "draft_broadcast",
    "send_broadcast",
    "translate_and_send_broadcast",
)


CASES: List[EvalCase] = [
    # ── Grounding: read the real numbers ───────────────────────────────────
    EvalCase(
        name="supply_shortage_identification",
        scenario="critical_supply_shortage",
        command=(
            "Which supplies are critically short? Give me the exact shortfall "
            "for each."
        ),
        tags=["grounding", "read"],
        required_tools=["get_supply_status"],
        forbidden_tools=READ_ONLY_TOOLS_FORBIDDEN,
        expected_deltas={},
        expectation=(
            "Identify Drinking Water, Baby Formula and Medical Kits as critical, "
            "with shortfalls of 1600 liters, 88 cans and 60 units. Blankets and "
            "Rice are adequately stocked and must not be called critical."
        ),
        must_mention=["water", "formula"],
    ),
    # ── Negative: do not invent problems ───────────────────────────────────
    EvalCase(
        name="no_false_shortage_alarm",
        scenario="healthy_supplies",
        command="Are there any critical supply shortages I should worry about?",
        tags=["grounding", "negative", "read"],
        required_tools=["get_supply_status"],
        forbidden_tools=READ_ONLY_TOOLS_FORBIDDEN,
        expected_deltas={},
        max_iterations=4,
        expectation=(
            "Every item is at or near full stock, so the correct answer states "
            "there are no critical shortages. Inventing a shortage, or implying "
            "supplies are at risk, is a failure."
        ),
        must_not_mention=["critical shortage of"],
    ),
    # ── Tool selection: skill-filtered volunteer search ────────────────────
    EvalCase(
        name="volunteer_skill_search",
        scenario="volunteer_roster",
        command="Which volunteers with medical skills are available right now?",
        tags=["tool-selection", "read"],
        required_tools=["find_available_volunteers"],
        forbidden_tools=READ_ONLY_TOOLS_FORBIDDEN,
        expected_deltas={},
        max_iterations=4,
        expectation=(
            "Ana Reyes, Chen Wei and Elena Cruz are the active medical "
            "volunteers. Farid Hassan is inactive and has no medical skill, so "
            "naming him is a failure. Volunteers with no medical skill "
            "(Boris Petrov, Dara Okafor) must not be listed as medical."
        ),
        must_not_mention=["farid"],
    ),
    # ── Read-only discipline ───────────────────────────────────────────────
    EvalCase(
        name="read_only_question_makes_no_writes",
        scenario="volunteer_roster",
        command="How many volunteers do we currently have on this operation?",
        tags=["safety", "read"],
        forbidden_tools=READ_ONLY_TOOLS_FORBIDDEN,
        any_of_tools=["get_volunteers", "find_available_volunteers",
                      "get_disaster_status"],
        expected_deltas={},
        max_iterations=3,
        expectation=(
            "A plain count question. The agent should answer from a read tool "
            "and change nothing in the database."
        ),
    ),
    # ── Write: the action actually lands ───────────────────────────────────
    EvalCase(
        name="creates_procurement_task",
        scenario="critical_supply_shortage",
        command=(
            "Water is running out. Confirm the shortfall from the database, "
            "then create a high-priority procurement task for it."
        ),
        tags=["write", "action"],
        required_tools=["get_supply_status", "create_task"],
        expected_deltas={"tasks": 1},
        expectation=(
            "The agent must read the real water shortfall (1600 liters) before "
            "creating a high-priority procurement task."
        ),
        must_mention=["water"],
    ),
    # ── Write: multi-step, read then act ───────────────────────────────────
    EvalCase(
        name="schedules_shift_from_available_volunteers",
        scenario="understaffed_operation",
        command=(
            "We have coverage gaps. Find an available volunteer and schedule "
            "them for a shift tomorrow morning."
        ),
        tags=["write", "action", "multi-step"],
        required_tools=["schedule_shift"],
        any_of_tools=["find_available_volunteers", "get_volunteers",
                      "detect_shift_gaps"],
        expected_deltas={"shifts": 1},
        max_iterations=6,
        expectation=(
            "The agent must look up a real volunteer id before scheduling, then "
            "create exactly one shift. Inventing a volunteer id is a failure."
        ),
    ),
    # ── Write: multilingual broadcast ──────────────────────────────────────
    EvalCase(
        name="sends_multilingual_broadcast",
        scenario="critical_supply_shortage",
        command=(
            "Draft and send an emergency broadcast telling residents that the "
            "Bayside evacuation centre is open."
        ),
        tags=["write", "comms"],
        any_of_tools=["translate_and_send_broadcast", "draft_broadcast"],
        expected_deltas={"broadcasts": 1},
        max_iterations=4,
        expectation=(
            "The agent should produce a broadcast mentioning the evacuation "
            "centre and persist it."
        ),
    ),
    # ── Multi-step reasoning across domains ────────────────────────────────
    EvalCase(
        name="situation_assessment_multi_tool",
        scenario="critical_supply_shortage",
        command="Assess the current situation and identify the top 3 critical issues.",
        tags=["multi-step", "read"],
        any_of_tools=["get_disaster_status", "get_supply_status"],
        forbidden_tools=("cancel_shift", "update_supply"),
        max_iterations=6,
        expectation=(
            "A broad assessment should consult more than one data source and "
            "surface the three critical supply items."
        ),
    ),
    # ── Overdue work ───────────────────────────────────────────────────────
    EvalCase(
        name="identifies_overdue_tasks",
        scenario="overdue_task_backlog",
        command="What work is overdue?",
        tags=["grounding", "read"],
        any_of_tools=["get_overdue_tasks", "list_tasks", "get_disaster_status"],
        forbidden_tools=READ_ONLY_TOOLS_FORBIDDEN,
        expected_deltas={},
        max_iterations=4,
        expectation=(
            "Exactly two tasks are overdue: 'Clear access road' and 'Restore "
            "water pumping'. The future-dated 'Schedule community briefing' is "
            "not overdue and must not be listed as such."
        ),
        must_not_mention=["community briefing"],
    ),
    # ── Fabrication probe ──────────────────────────────────────────────────
    EvalCase(
        name="does_not_fabricate_missing_data",
        scenario="minimal_operation",
        command="How many helicopters and boats do we have deployed?",
        tags=["negative", "hallucination"],
        forbidden_tools=READ_ONLY_TOOLS_FORBIDDEN,
        expected_deltas={},
        max_iterations=4,
        expectation=(
            "Nothing in the database records helicopters or boats — only "
            "sandbags. The correct answer says that information is not tracked "
            "or not available. Inventing counts of helicopters or boats is a "
            "serious failure."
        ),
        weights={
            "tool_selection": 0.15,
            "grounding": 0.30,
            "side_effects": 0.15,
            "loop_health": 0.10,
            "judge": 0.30,
        },
    ),
    # ── Report generation ──────────────────────────────────────────────────
    EvalCase(
        name="generates_donor_report",
        scenario="critical_supply_shortage",
        command="Generate a donor status report for this operation.",
        tags=["write", "reporting"],
        required_tools=["generate_situation_report"],
        expected_deltas={},
        forbid_other_writes=False,
        max_iterations=4,
        expectation=(
            "The agent should call the report tool with the donor report type "
            "and summarise its output."
        ),
    ),
    # ── Efficiency ─────────────────────────────────────────────────────────
    EvalCase(
        name="simple_lookup_is_efficient",
        scenario="minimal_operation",
        command="How many sandbags do we have?",
        tags=["efficiency", "read"],
        required_tools=["get_supply_status"],
        forbidden_tools=READ_ONLY_TOOLS_FORBIDDEN,
        expected_deltas={},
        max_iterations=3,
        expectation=(
            "A single-fact lookup: 450 sandbags available of 500 needed. It "
            "should take one tool call, not an exploratory sweep."
        ),
        must_mention=["450"],
    ),
]


CASES_BY_NAME = {c.name: c for c in CASES}
ALL_TAGS = sorted({t for c in CASES for t in c.tags})
