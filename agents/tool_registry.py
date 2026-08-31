"""
Tool registry — adapts the FastMCP tool server into the schema/execute pair the
LLM tool-calling loop needs.

There is exactly one implementation of every tool: the functions registered on
the FastMCP instance in ``mcp_server.tools``. This module does not reimplement
any of them. It only:

  * translates MCP tool definitions into the provider's function-calling schema
  * dispatches a model-generated tool call back through ``mcp.call_tool``
  * turns failures into structured tool results instead of exceptions

Because dispatch goes through ``mcp.call_tool``, argument validation, and result
serialization are performed by MCP itself — the same path an external MCP client
would take.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple

from mcp_server.tools import mcp

# Tool results larger than this are truncated before being handed back to the
# model, so one oversized query cannot blow the context window.
MAX_TOOL_RESULT_CHARS = 8_000


class ToolExecutionError(Exception):
    """Raised only for failures that should abort the run, never for tool errors."""


# ---------------------------------------------------------------------------
# Schema generation
# ---------------------------------------------------------------------------


async def get_tool_schemas() -> List[Dict[str, Any]]:
    """
    Return every MCP tool as an OpenAI/Groq-compatible function schema.

    ``FunctionTool.parameters`` is already a JSON Schema object generated from
    the Python signature, so it is passed through unchanged.
    """
    tools = await mcp.list_tools()
    schemas: List[Dict[str, Any]] = []

    for tool in sorted(tools, key=lambda t: t.name):
        params = tool.parameters or {"type": "object", "properties": {}}
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": (tool.description or "").strip(),
                    "parameters": params,
                },
            }
        )

    return schemas


async def get_tool_names() -> List[str]:
    """Names of all registered tools."""
    return sorted(t.name for t in await mcp.list_tools())


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _truncate(payload: str) -> Tuple[str, bool]:
    if len(payload) <= MAX_TOOL_RESULT_CHARS:
        return payload, False
    keep = MAX_TOOL_RESULT_CHARS
    return (
        payload[:keep] + f"\n…[truncated {len(payload) - keep} chars]",
        True,
    )


def _unwrap(result: Any) -> Any:
    """
    Normalise a FastMCP ``ToolResult`` into plain JSON-serialisable data.

    FastMCP wraps a bare (non-dict) return value as ``{"result": ...}`` in
    ``structured_content``; unwrap that so the model sees the tool's own shape.
    """
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        if set(structured.keys()) == {"result"}:
            return structured["result"]
        return structured

    content = getattr(result, "content", None)
    if content:
        text = getattr(content[0], "text", None)
        if text is not None:
            try:
                return json.loads(text)
            except (ValueError, TypeError):
                return text

    return structured if structured is not None else None


def parse_tool_arguments(raw_arguments: Any) -> Dict[str, Any]:
    """
    Parse the model-supplied argument blob.

    Models return arguments as a JSON string; a dict is accepted too. Anything
    that is neither is a malformed call.
    """
    if raw_arguments is None or raw_arguments == "":
        return {}
    if isinstance(raw_arguments, dict):
        return raw_arguments
    if isinstance(raw_arguments, str):
        try:
            parsed = json.loads(raw_arguments)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"arguments were not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError(
                f"arguments must decode to an object, got {type(parsed).__name__}"
            )
        return parsed
    raise ValueError(f"unsupported argument type {type(raw_arguments).__name__}")


async def execute_tool(
    name: str,
    raw_arguments: Any,
    known_tools: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Execute one model-generated tool call.

    Never raises for an unknown tool, malformed arguments, or a tool that throws
    — each returns a structured error so the model can read it and recover.

    Returns a trace record::

        {tool, arguments, ok, result | error, duration_ms, truncated}
    """
    started = time.perf_counter()

    def _record(**extra: Any) -> Dict[str, Any]:
        return {
            "tool": name,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            **extra,
        }

    # 1. Validate the tool name.
    names = known_tools if known_tools is not None else await get_tool_names()
    if name not in names:
        return _record(
            arguments={},
            ok=False,
            error=(
                f"Unknown tool {name!r}. Available tools: {', '.join(names)}."
            ),
        )

    # 2. Validate the arguments.
    try:
        arguments = parse_tool_arguments(raw_arguments)
    except ValueError as exc:
        return _record(
            arguments={},
            ok=False,
            error=f"Invalid arguments for {name!r}: {exc}",
        )

    # 3. Execute through MCP (which validates against the tool's schema).
    try:
        raw_result = await mcp.call_tool(name, arguments)
    except Exception as exc:  # noqa: BLE001 - surfaced to the model, not swallowed
        return _record(
            arguments=arguments,
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
        )

    # 4. Serialise and cap the result.
    value = _unwrap(raw_result)
    try:
        payload = json.dumps(value, default=str)
    except (TypeError, ValueError) as exc:
        return _record(
            arguments=arguments,
            ok=False,
            error=f"Tool result was not serialisable: {exc}",
        )

    payload, truncated = _truncate(payload)

    return _record(
        arguments=arguments,
        ok=True,
        result=payload,
        truncated=truncated,
    )
