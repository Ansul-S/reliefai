"""
ReliefAI Orchestrator — tool-using agent loop.

The orchestrator is a single LLM agent with access to the ReliefAI MCP tool
server. It decides which tools it needs, calls them through the provider's
native function-calling API, reads the results, and keeps calling tools until
it has enough information to answer. Only then does it emit the structured
response the API and dashboard consume.

    user command
         │
         ▼
    ┌─────────┐   tool_calls   ┌──────────────┐
    │   LLM   │ ─────────────▶ │ MCP tools    │
    │         │ ◀───────────── │ (registry)   │
    └────┬────┘   tool results └──────────────┘
         │  (repeats until the model stops requesting tools,
         │   bounded by MAX_TOOL_ITERATIONS)
         ▼
    structured final response

There is no hardcoded agent dispatch and no keyword routing: which tools run,
in what order, and how many times is entirely the model's decision.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

import database.crud as crud
from database.models import SEVERITY_SCALE_MAX
from agents._groq_client import (
    _call_groq_with_tools,
    estimate_cost_usd,
    get_model_name,
    track_usage,
)
from agents.tool_registry import execute_tool, get_tool_names, get_tool_schemas

_AGENT_NAME = "orchestrator"

# Hard stop on the tool loop. Configurable, but always at least 1.
MAX_TOOL_ITERATIONS = max(1, int(os.environ.get("MAX_TOOL_ITERATIONS", "8")))

# How many times the model may issue a byte-identical tool call before we stop
# executing it and tell the model it already has that result.
MAX_DUPLICATE_TOOL_CALLS = 2

_SYSTEM_PROMPT = (
    "You are ReliefAI Orchestrator, the coordination agent for a post-disaster "
    "relief operation.\n\n"
    "You have tools that read and modify the live operations database. Use them. "
    "Never guess at numbers, volunteer names, supply levels or shift coverage — "
    "call a tool and read the real value. Call as many tools as you need, and "
    "call them again with different arguments if the first result is "
    "insufficient. When several independent facts are needed, request the "
    "corresponding tools together in one turn so they run concurrently.\n\n"
    "An empty result is an answer, not a failure. A result that is merely "
    "empty is not insufficient, and rephrasing the query will not make absent "
    "data appear: when a source comes back empty twice, the absence is "
    "established — stop querying it.\n\n"
    "Never convert an absence into a measurement. If the operation does not "
    "track something, say that it is not tracked. \"No helicopters are "
    "recorded for this operation\" is true; \"the system records 0 "
    "helicopters deployed\" is a fabrication, because nothing was ever "
    "counted. A count of zero is a real reading that a tool returned; an empty "
    "table is the absence of any reading at all.\n\n"
    "Typical approach: establish the situation first, then investigate the "
    "specific areas the command concerns, then take corrective action (creating "
    "tasks, scheduling shifts, sending broadcasts) when the command calls for "
    "it.\n\n"
    "Be decisive and operational. Lives depend on speed. When you have enough "
    "information, stop calling tools and write your answer."
)

_FINAL_RESPONSE_INSTRUCTION = (
    "Now produce your final answer as a single JSON object with exactly these "
    "keys:\n"
    '  "orchestrator_summary": string — what you found and did, 2-4 sentences, '
    "citing concrete numbers from the tool results\n"
    '  "alerts": array of strings — urgent issues needing human attention now\n'
    '  "next_recommended_actions": array of strings — concrete next steps\n\n'
    "Base every statement on the tool results above. If the command asks about "
    "something the operation does not track, say so plainly rather than "
    "reporting a count of zero for it. Respond with ONLY the JSON object, no "
    "markdown fences and no commentary."
)


def _tool_call_signature(name: str, raw_args: Any) -> str:
    """Stable identity for a tool call, used to detect exact repeats."""
    if isinstance(raw_args, str):
        try:
            raw_args = json.loads(raw_args)
        except (ValueError, TypeError):
            return f"{name}:{raw_args}"
    try:
        return f"{name}:{json.dumps(raw_args, sort_keys=True, default=str)}"
    except (TypeError, ValueError):
        return f"{name}:{raw_args}"


def describe_disaster(disaster: Any) -> str:
    """
    The operation facts the agent is told before it reads any tool.

    Exposed because anything scoring the agent needs the same facts. An eval
    judge that sees only tool payloads reads "Cyclone Vega" as an invention and
    marks a true statement unsupported — the same failure as a constant that
    lives in a docstring instead of a payload, one layer up.
    """
    return (
        f"Disaster id={disaster.id} — {disaster.name!r} "
        f"({disaster.type}, {disaster.location}, "
        f"severity {disaster.severity}/{SEVERITY_SCALE_MAX}, "
        f"status {disaster.status})."
    )


def _build_user_message(disaster: Any, command: str) -> str:
    return f"{describe_disaster(disaster)}\n\nCOMMAND: {command}"


def _unique_result_key(results: Dict[str, Any], tool_name: str) -> str:
    """Key results by tool name, suffixing repeats so none are overwritten."""
    if tool_name not in results:
        return tool_name
    n = 2
    while f"{tool_name}#{n}" in results:
        n += 1
    return f"{tool_name}#{n}"


def _decode_result(payload: Any) -> Any:
    """Tool results are stored as JSON strings; decode for the API response."""
    if not isinstance(payload, str):
        return payload
    try:
        return json.loads(payload)
    except (ValueError, TypeError):
        return payload


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, default=str)


def _as_text_list(value: Any) -> List[str]:
    """
    Coerce a contract list field to a list of strings.

    Models sometimes return a bare string where a list was asked for, or an
    object per alert. Both carry a real answer, so they are kept as text
    rather than failing the run; the dashboard and the scorers can then rely
    on a list of strings.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [_as_text(v) for v in value if v is not None and v != ""]
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [_as_text(value)]


def _validate_answer(parsed: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[str]]:
    """
    Hold the final answer to its contract: a non-empty summary string, and
    ``alerts`` / ``next_recommended_actions`` as lists of strings.

    A missing summary is an error, not an empty success — the API would
    otherwise report a run as successful with no answer in it.
    """
    summary = parsed.get("orchestrator_summary")
    if not isinstance(summary, str) or not summary.strip():
        return {}, "Final response had no orchestrator_summary string."
    return {
        "orchestrator_summary": summary,
        "alerts": _as_text_list(parsed.get("alerts")),
        "next_recommended_actions": _as_text_list(
            parsed.get("next_recommended_actions")
        ),
    }, None


async def _run_tool_calls(
    tool_calls: List[Dict[str, Any]],
    known_tools: List[str],
    seen: Dict[str, int],
) -> List[Dict[str, Any]]:
    """
    Execute every tool call the model requested in this turn, concurrently.

    Exact repeats beyond MAX_DUPLICATE_TOOL_CALLS are short-circuited with a
    message telling the model it already has that result, rather than paying for
    the same query again.
    """

    async def _one(call: Dict[str, Any]) -> Dict[str, Any]:
        fn = call.get("function", {})
        name = fn.get("name", "")
        raw_args = fn.get("arguments", "{}")

        signature = _tool_call_signature(name, raw_args)
        seen[signature] = seen.get(signature, 0) + 1
        if seen[signature] > MAX_DUPLICATE_TOOL_CALLS:
            # A refusal is still the answer to this tool call, so it carries the
            # call's id: every tool_call_id in the assistant turn must be
            # answered by a tool message bearing the same id.
            return {
                "tool": name,
                "tool_call_id": call.get("id"),
                "arguments": {},
                "ok": False,
                "duration_ms": 0,
                "error": (
                    f"Repeated call to {name!r} with identical arguments was "
                    "skipped. You already have this result above — use it "
                    "instead of calling again."
                ),
            }

        # Two tools call a model themselves (the report and comms agents).
        # Metering each call separately is what lets the run's usage include
        # them, and lets the trace say which tool spent the tokens.
        with track_usage() as meter:
            record = await execute_tool(name, raw_args, known_tools=known_tools)
        record["tool_call_id"] = call.get("id")
        if meter["calls"]:
            record["model_usage"] = dict(meter)
        return record

    return await asyncio.gather(*(_one(c) for c in tool_calls))


async def _finalise(
    messages: List[Dict[str, Any]],
    usage_totals: Dict[str, int],
) -> Tuple[Dict[str, Any], Optional[str]]:
    """
    Ask the model for the structured response, with tools disabled.

    Returns ``(parsed, error)``. On unparseable output ``parsed`` is an empty
    dict and ``error`` explains why — the caller degrades gracefully rather than
    returning malformed data.
    """
    messages = messages + [{"role": "user", "content": _FINAL_RESPONSE_INSTRUCTION}]

    turn = await _call_groq_with_tools(
        messages,
        tools=[],
        response_format={"type": "json_object"},
    )
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        usage_totals[key] += turn["usage"][key]
    usage_totals["llm_ms"] += turn["latency_ms"]
    usage_totals["retry_wait_ms"] += turn.get("retry_wait_ms", 0)

    content = (turn["message"].get("content") or "").strip()
    if not content:
        return {}, "Model returned an empty final response."

    try:
        parsed = json.loads(content)
    except (ValueError, TypeError) as exc:
        # Recovery: pull the outermost JSON object out of any surrounding prose.
        start, end = content.find("{"), content.rfind("}")
        if start != -1 and end > start:
            try:
                parsed = json.loads(content[start : end + 1])
            except (ValueError, TypeError):
                return {}, f"Final response was not valid JSON: {exc}"
        else:
            return {}, f"Final response was not valid JSON: {exc}"

    if not isinstance(parsed, dict):
        return {}, "Final response JSON was not an object."

    return _validate_answer(parsed)


async def run_orchestrator(
    disaster_id: int,
    command: str,
    db: AsyncSession,
) -> Dict[str, Any]:
    """
    Run the tool-calling loop for *command* against *disaster_id*.

    Returns the same response shape the API and dashboard have always consumed
    (``orchestrator_summary``, ``agents_called``, ``results``, ``alerts``,
    ``next_recommended_actions``, ``execution_time_ms``), plus ``run_notices``
    for remarks about the run itself and a ``trace`` with per-tool timings and
    token accounting.
    """
    start_ts = time.time()
    # input/output/total_tokens are the orchestrator's own turns, comparable
    # across runs. tool_model_* is what tools that call a model spent on top.
    usage_totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "tool_model_input_tokens": 0,
        "tool_model_output_tokens": 0,
        "tool_model_tokens": 0,
        "llm_ms": 0,
        "tool_ms": 0,
        "retry_wait_ms": 0,
    }

    def _run_cost() -> Optional[float]:
        """Every model call this command caused, including those inside tools."""
        return estimate_cost_usd(
            usage_totals["input_tokens"] + usage_totals["tool_model_input_tokens"],
            usage_totals["output_tokens"] + usage_totals["tool_model_output_tokens"],
        )
    trace: List[Dict[str, Any]] = []
    results: Dict[str, Any] = {}
    tools_called: List[str] = []

    async def _log_failure(message: str) -> Dict[str, Any]:
        duration_ms = int((time.time() - start_ts) * 1000)
        await crud.create_log(
            db,
            agent_name=_AGENT_NAME,
            action="run_orchestrator",
            input_summary=f"disaster_id={disaster_id} command={command[:120]}",
            output_summary=f"ERROR: {message}",
            status="error",
            duration_ms=duration_ms,
            disaster_id=disaster_id,
            model=get_model_name(),
            input_tokens=usage_totals["input_tokens"],
            output_tokens=usage_totals["output_tokens"],
            total_tokens=usage_totals["total_tokens"],
            cost_usd=_run_cost(),
            trace=trace,
        )
        await db.commit()
        return {"error": message, "agent": _AGENT_NAME}

    disaster = await crud.get_disaster(db, disaster_id)
    if disaster is None:
        return await _log_failure(f"Disaster {disaster_id} not found.")

    try:
        tool_schemas = await get_tool_schemas()
        known_tools = await get_tool_names()
    except Exception as exc:  # noqa: BLE001
        return await _log_failure(f"Could not load tools: {exc}")

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": _build_user_message(disaster, command)},
    ]

    seen_signatures: Dict[str, int] = {}
    iterations = 0
    hit_iteration_limit = False

    # ── Tool-calling loop ──────────────────────────────────────────────────
    while True:
        if iterations >= MAX_TOOL_ITERATIONS:
            hit_iteration_limit = True
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"You have reached the maximum of {MAX_TOOL_ITERATIONS} "
                        "tool-calling rounds. Stop calling tools and answer with "
                        "what you already have."
                    ),
                }
            )
            break

        iterations += 1

        try:
            turn = await _call_groq_with_tools(messages, tool_schemas)
        except Exception as exc:  # noqa: BLE001
            return await _log_failure(f"LLM call failed: {exc}")

        for key in ("input_tokens", "output_tokens", "total_tokens"):
            usage_totals[key] += turn["usage"][key]
        usage_totals["llm_ms"] += turn["latency_ms"]
        usage_totals["retry_wait_ms"] += turn.get("retry_wait_ms", 0)
        trace.append(
            {
                "step": "llm",
                "iteration": iterations,
                "duration_ms": turn["latency_ms"],
                "finish_reason": turn["finish_reason"],
                "tokens": turn["usage"]["total_tokens"],
                # Backoff is reported beside the latency, not hidden inside it.
                **(
                    {
                        "attempts": turn["attempts"],
                        "retry_wait_ms": turn.get("retry_wait_ms", 0),
                    }
                    if turn.get("attempts", 1) > 1
                    else {}
                ),
            }
        )

        assistant_message = turn["message"]
        tool_calls = assistant_message.get("tool_calls")

        if not tool_calls:
            # Model is done gathering information.
            messages.append(
                {
                    "role": "assistant",
                    "content": assistant_message.get("content") or "",
                }
            )
            break

        messages.append(assistant_message)

        records = await _run_tool_calls(tool_calls, known_tools, seen_signatures)

        for record in records:
            usage_totals["tool_ms"] += record["duration_ms"]
            model_usage = record.get("model_usage")
            if model_usage:
                usage_totals["tool_model_input_tokens"] += model_usage["input_tokens"]
                usage_totals["tool_model_output_tokens"] += model_usage["output_tokens"]
                usage_totals["tool_model_tokens"] += model_usage["total_tokens"]
            tools_called.append(record["tool"])
            trace.append(
                {
                    "step": "tool",
                    "iteration": iterations,
                    "tool": record["tool"],
                    "arguments": record.get("arguments", {}),
                    "duration_ms": record["duration_ms"],
                    "ok": record["ok"],
                    **({"error": record["error"]} if not record["ok"] else {}),
                    **(
                        {"model_tokens": model_usage["total_tokens"]}
                        if model_usage
                        else {}
                    ),
                }
            )

            if record["ok"]:
                content = record["result"]
                results[_unique_result_key(results, record["tool"])] = _decode_result(
                    record["result"]
                )
            else:
                content = json.dumps({"error": record["error"]})
                results[_unique_result_key(results, record["tool"])] = {
                    "error": record["error"]
                }

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": record.get("tool_call_id") or record["tool"],
                    "name": record["tool"],
                    "content": content,
                }
            )

    # ── Structured final response ──────────────────────────────────────────
    try:
        parsed, parse_error = await _finalise(messages, usage_totals)
    except Exception as exc:  # noqa: BLE001
        return await _log_failure(f"Final response generation failed: {exc}")

    if parse_error:
        return await _log_failure(parse_error)

    duration_ms = int((time.time() - start_ts) * 1000)
    cost_usd = _run_cost()

    alerts = parsed["alerts"]

    # Notices about how the run itself went, kept out of `alerts`. Alerts are
    # the agent's claims about the operation; a notice is the harness talking
    # about its own execution. Mixing them made the orchestrator's own round
    # limit read as an unsupported factual claim by the agent — both to a
    # reader and to the grounding scorer, which checks every number in
    # `alerts` against tool output and cannot trace a number we wrote.
    run_notices: List[str] = []
    if hit_iteration_limit:
        run_notices.append(
            f"Orchestrator stopped at the {MAX_TOOL_ITERATIONS}-round tool limit; "
            "the answer may be incomplete."
        )

    failed_tools = sum(1 for t in trace if t["step"] == "tool" and not t["ok"])

    await crud.create_log(
        db,
        agent_name=_AGENT_NAME,
        action="run_orchestrator",
        input_summary=f"disaster_id={disaster_id} command={command[:120]}",
        output_summary=(
            f"tools={tools_called} iterations={iterations} "
            f"failed_tools={failed_tools} "
            f"summary={str(parsed.get('orchestrator_summary', ''))[:120]}"
        ),
        status="success" if failed_tools == 0 else "error",
        duration_ms=duration_ms,
        disaster_id=disaster_id,
        model=get_model_name(),
        input_tokens=usage_totals["input_tokens"],
        output_tokens=usage_totals["output_tokens"],
        total_tokens=usage_totals["total_tokens"],
        cost_usd=cost_usd,
        trace=trace,
    )
    await db.commit()

    return {
        "orchestrator_summary": parsed["orchestrator_summary"],
        # Kept for API/dashboard compatibility: now the tools the model chose,
        # rather than a fixed roster of prompt agents.
        "agents_called": tools_called,
        "tools_called": tools_called,
        "results": results,
        "alerts": alerts,
        "run_notices": run_notices,
        "next_recommended_actions": parsed["next_recommended_actions"],
        "execution_time_ms": duration_ms,
        "trace": trace,
        "iterations": iterations,
        "model": get_model_name(),
        "usage": {
            "input_tokens": usage_totals["input_tokens"],
            "output_tokens": usage_totals["output_tokens"],
            "total_tokens": usage_totals["total_tokens"],
            "tool_model_tokens": usage_totals["tool_model_tokens"],
            "llm_ms": usage_totals["llm_ms"],
            "tool_ms": usage_totals["tool_ms"],
            "retry_wait_ms": usage_totals["retry_wait_ms"],
            "estimated_cost_usd": cost_usd,
        },
    }
