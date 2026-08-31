"""Tests for the MCP-backed tool registry."""
from __future__ import annotations

import json

import pytest

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
