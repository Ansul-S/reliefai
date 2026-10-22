"""Tests for the MCP-backed tool registry."""
from __future__ import annotations

import json

import pytest

import database.crud as crud
from database.models import SEVERITY_SCALE_MAX
from agents.tool_registry import (
    MAX_TOOL_RESULT_CHARS,
    execute_tool,
    get_tool_names,
    get_tool_schemas,
    parse_tool_arguments,
)


# ---------------------------------------------------------------------------
# Discovery and schema generation
# ---------------------------------------------------------------------------


async def test_tools_are_discovered():
    names = await get_tool_names()
    assert len(names) >= 15
    # Tools spanning every domain area should be present.
    for expected in (
        "get_disaster_status",
        "get_supply_status",
        "find_available_volunteers",
        "schedule_shift",
        "create_task",
        "translate_and_send_broadcast",
    ):
        assert expected in names


async def test_schemas_are_valid_function_definitions():
    schemas = await get_tool_schemas()
    assert schemas

    for schema in schemas:
        assert schema["type"] == "function"
        fn = schema["function"]
        assert fn["name"]
        assert fn["description"], f"{fn['name']} has no description"
        params = fn["parameters"]
        assert params["type"] == "object"
        assert "properties" in params


async def test_schema_declares_parameter_types_and_required():
    schemas = await get_tool_schemas()
    supply = next(
        s for s in schemas if s["function"]["name"] == "get_supply_status"
    )
    params = supply["function"]["parameters"]
    assert params["properties"]["disaster_id"]["type"] == "integer"
    assert params["required"] == ["disaster_id"]


async def test_optional_parameters_are_not_required():
    schemas = await get_tool_schemas()
    tool = next(
        s for s in schemas if s["function"]["name"] == "find_available_volunteers"
    )
    params = tool["function"]["parameters"]
    assert "disaster_id" in params.get("required", [])
    assert "required_skills" not in params.get("required", [])


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def test_parse_arguments_accepts_json_string_and_dict():
    assert parse_tool_arguments('{"a": 1}') == {"a": 1}
    assert parse_tool_arguments({"a": 1}) == {"a": 1}
    assert parse_tool_arguments("") == {}
    assert parse_tool_arguments(None) == {}


def test_parse_arguments_rejects_malformed_json():
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_tool_arguments("{not json")


def test_parse_arguments_rejects_non_object_json():
    with pytest.raises(ValueError, match="must decode to an object"):
        parse_tool_arguments("[1, 2, 3]")


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


async def test_valid_tool_execution(seeded_disaster):
    record = await execute_tool(
        "get_supply_status",
        json.dumps({"disaster_id": seeded_disaster["disaster_id"]}),
    )
    assert record["ok"] is True
    assert record["tool"] == "get_supply_status"
    assert isinstance(record["duration_ms"], int)

    supplies = json.loads(record["result"])
    assert any(s["item_name"] == "Drinking Water" for s in supplies)
    # 100 of 1000 is under the 30% critical threshold.
    assert any(s["is_critical"] for s in supplies)


async def test_supply_payload_carries_the_threshold_that_classified_it(
    seeded_disaster,
):
    """
    The agent explains criticality to operators ("below the 30 % threshold").
    That figure has to come from the payload, not from the docstring, or it
    goes stale silently and cannot be traced by the grounding scorer.
    """
    record = await execute_tool(
        "get_supply_status",
        json.dumps({"disaster_id": seeded_disaster["disaster_id"]}),
    )
    supplies = json.loads(record["result"])
    assert supplies
    assert all(s["critical_threshold_pct"] == 30 for s in supplies)


async def test_supply_payload_lists_high_priority_first(db):
    """
    Regression: priority is stored as text, and ordering it descending gave
    medium, low, high. The most urgent items came last, so they were the first
    cut when a payload was truncated.
    """
    disaster = await crud.create_disaster(db, "Priority Probe", "flood", "Testville", 2)
    for name, priority in (
        ("Antibiotics", "low"),
        ("Blankets", "high"),
        ("Cots", "medium"),
        ("Diesel", "high"),
        ("Ether", "unrated"),
    ):
        await crud.create_supply(db, disaster.id, name, "medical", 100.0, 50.0, "units", priority)
    await db.commit()

    record = await execute_tool(
        "get_supply_status", json.dumps({"disaster_id": disaster.id})
    )
    supplies = json.loads(record["result"])

    assert [(s["priority"], s["item_name"]) for s in supplies] == [
        ("high", "Blankets"),
        ("high", "Diesel"),
        ("medium", "Cots"),
        ("low", "Antibiotics"),
        ("unrated", "Ether"),
    ]


async def test_gap_payload_carries_its_scan_parameters(seeded_disaster):
    """detect_shift_gaps reports the window it scanned, not just what it found."""
    record = await execute_tool(
        "detect_shift_gaps",
        json.dumps({"disaster_id": seeded_disaster["disaster_id"]}),
    )
    assert record["ok"] is True

    payload = json.loads(record["result"])
    assert payload["window_days"] == crud.SHIFT_GAP_WINDOW_DAYS
    assert payload["block_hours"] == crud.SHIFT_GAP_BLOCK_HOURS
    assert payload["min_volunteers"] == crud.SHIFT_GAP_MIN_VOLUNTEERS
    assert payload["gap_count"] == len(payload["gaps"])
    # The window survives an empty result, which is exactly the case where an
    # agent says "no gaps over the next 7 days" and has nothing to cite.
    assert isinstance(payload["gaps"], list)


async def _gap_fixture(db, monkeypatch, now):
    """A fresh disaster with the scan's clock fixed at *now*."""
    from datetime import timedelta

    monkeypatch.setattr(crud, "_utcnow", lambda: now)
    disaster = await crud.create_disaster(db, "Gap Probe", "flood", "Testville", 2)
    volunteer = await crud.create_volunteer(db, "Solo Volunteer", "+10000000000", "solo@example.com")
    await db.commit()
    return disaster.id, volunteer.id, timedelta


async def test_gap_scan_starts_at_the_block_in_progress(db, monkeypatch):
    """
    Regression: the window started at 00:00 today, so blocks that had already
    ended were reported as gaps to fill.
    """
    from datetime import datetime, timezone

    now = datetime(2030, 1, 1, 10, 30, tzinfo=timezone.utc)
    did, _, _ = await _gap_fixture(db, monkeypatch, now)

    gaps = await crud.detect_shift_gaps(db, did)

    assert (gaps[0]["date"], gaps[0]["hour_block"]) == ("2030-01-01", 8)
    assert len(gaps) == crud.SHIFT_GAP_WINDOW_DAYS * 24 // crud.SHIFT_GAP_BLOCK_HOURS


async def test_gap_scan_counts_people_not_shifts(db, monkeypatch):
    """
    Regression: one volunteer holding two overlapping shifts counted as two,
    which closed a gap that a single person was covering.
    """
    from datetime import datetime, timezone

    now = datetime(2030, 1, 1, 8, 0, tzinfo=timezone.utc)
    did, vid, timedelta = await _gap_fixture(db, monkeypatch, now)
    for role in ("Triage", "Logistics"):
        await crud.create_shift(db, did, vid, role, now, now + timedelta(hours=4))
    await db.commit()

    gaps = await crud.detect_shift_gaps(db, did)
    first = gaps[0]

    assert (first["date"], first["hour_block"]) == ("2030-01-01", 8)
    assert first["volunteers_count"] == 1


def _shift_args(seeded_disaster, **overrides):
    return json.dumps({
        "disaster_id": seeded_disaster["disaster_id"],
        "volunteer_id": seeded_disaster["volunteer_id"],
        "role": "Triage",
        "start_time": "2030-01-01T08:00:00+00:00",
        "end_time": "2030-01-01T12:00:00+00:00",
        **overrides,
    })


async def test_schedule_shift_books_a_known_volunteer(seeded_disaster):
    record = await execute_tool("schedule_shift", _shift_args(seeded_disaster))
    shift = json.loads(record["result"])
    assert shift["volunteer_id"] == seeded_disaster["volunteer_id"]
    assert shift["volunteer_name"] == "Test Volunteer"


@pytest.mark.parametrize("field, label", [("volunteer_id", "Volunteer"), ("disaster_id", "Disaster")])
async def test_schedule_shift_refuses_ids_that_do_not_exist(db, seeded_disaster, field, label):
    """
    Regression: the tool booked volunteer 99999 on disaster 424242, neither of
    which existed, and the row persisted. A model-invented id has to come back
    as an error naming it, so the agent can correct itself.
    """
    args = _shift_args(seeded_disaster, **{field: 424242})
    record = await execute_tool("schedule_shift", args)

    assert json.loads(record["result"]) == {"error": f"{label} 424242 not found."}
    assert await crud.list_shifts_by_disaster(db, json.loads(args)["disaster_id"]) == []


async def test_disaster_status_carries_the_severity_scale(seeded_disaster):
    record = await execute_tool(
        "get_disaster_status",
        json.dumps({"disaster_id": seeded_disaster["disaster_id"]}),
    )
    status = json.loads(record["result"])
    assert status["severity_scale_max"] == SEVERITY_SCALE_MAX
    assert 1 <= status["severity"] <= status["severity_scale_max"]


async def test_unknown_tool_fails_safely():
    record = await execute_tool("definitely_not_a_tool", "{}")
    assert record["ok"] is False
    assert "Unknown tool" in record["error"]
    # The model is told what it *can* call, so it can recover.
    assert "get_supply_status" in record["error"]


async def test_malformed_arguments_fail_safely():
    record = await execute_tool("get_supply_status", "{{{ not json")
    assert record["ok"] is False
    assert "Invalid arguments" in record["error"]


async def test_missing_required_argument_fails_safely():
    record = await execute_tool("get_supply_status", "{}")
    assert record["ok"] is False
    assert record["error"]


async def test_tool_exception_is_captured_not_raised(monkeypatch):
    async def boom(name, arguments=None, **kwargs):
        raise RuntimeError("database exploded")

    monkeypatch.setattr("agents.tool_registry.mcp.call_tool", boom)

    record = await execute_tool("get_supply_status", '{"disaster_id": 1}')
    assert record["ok"] is False
    assert "database exploded" in record["error"]
    assert "RuntimeError" in record["error"]


async def test_tool_returning_error_dict_is_still_a_successful_call(seeded_disaster):
    """A tool's own domain error is data for the model, not a transport failure."""
    record = await execute_tool("get_disaster_status", '{"disaster_id": 999999}')
    assert record["ok"] is True
    assert "not found" in json.loads(record["result"])["error"]


async def test_large_results_are_truncated(monkeypatch, seeded_disaster):
    class FakeResult:
        structured_content = {"result": ["x" * 100] * 500}

    async def big(name, arguments=None, **kwargs):
        return FakeResult()

    monkeypatch.setattr("agents.tool_registry.mcp.call_tool", big)

    record = await execute_tool("get_supply_status", '{"disaster_id": 1}')
    assert record["ok"] is True
    assert record["truncated"] is True
    assert len(record["result"]) < MAX_TOOL_RESULT_CHARS + 200
    assert "truncated" in record["result"]
