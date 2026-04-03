"""
ReliefAI Orchestrator
Primary coordination agent — decomposes a high-level command into sub-agent
calls, runs parallel agents concurrently, and returns an aggregated result.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List

from sqlalchemy.ext.asyncio import AsyncSession

import database.crud as crud
from agents._groq_client import _call_groq

# Import sub-agents (lazy-safe — already imported by __init__)
from agents.volunteer_agent import run_volunteer_agent
from agents.supply_agent import run_supply_agent
from agents.comms_agent import run_comms_agent
from agents.shift_agent import run_shift_agent
from agents.report_agent import run_report_agent

_AGENT_NAME = "orchestrator"

_SYSTEM_PROMPT = (
    "You are ReliefAI Orchestrator — primary coordination agent for disaster "
    "relief operations. You receive high-level commands and decide which "
    "specialist sub-agents to call. "
    "Sub-agents available: volunteer_agent, supply_agent, comms_agent, "
    "shift_agent, report_agent. "
    "Analyze the command, identify all relevant sub-agents, plan their "
    "execution order. Some can run in parallel. "
    "Always think proactively about what could go wrong and include alerts. "
    "You must respond with ONLY valid JSON with these exact keys: "
    "{orchestrator_summary: str, agents_to_call: list[str], "
    "execution_plan: str, alerts: list[str], "
    "next_recommended_actions: list[str]}. "
    "No explanation, no markdown, no code blocks."
)

# Agents that can safely run in parallel (no write-write conflict)
_PARALLEL_SAFE = frozenset({"supply_agent", "volunteer_agent", "shift_agent"})

# Agents that must run after the parallel group (read results / generate output)
_SEQUENTIAL_AFTER = frozenset({"report_agent", "comms_agent"})


async def _invoke_agent(
    agent_name: str,
    disaster_id: int,
    command: str,
    db: AsyncSession,
) -> Dict[str, Any]:
    """
    Dispatch a single sub-agent by name.
    Returns the agent result dict or an error dict on failure.
    """
    try:
        if agent_name == "volunteer_agent":
            return await run_volunteer_agent(disaster_id, command, db)
        elif agent_name == "supply_agent":
            return await run_supply_agent(disaster_id, command, db)
        elif agent_name == "comms_agent":
            # Derive a sensible broadcast message from the command
            return await run_comms_agent(
                disaster_id,
                message=command,
                languages=["en", "hi", "ta", "id", "tl", "ja"],
                channel="all",
                db=db,
            )
        elif agent_name == "shift_agent":
            return await run_shift_agent(disaster_id, db)
        elif agent_name == "report_agent":
            return await run_report_agent(disaster_id, "internal", db)
        else:
            return {"error": f"Unknown agent: {agent_name!r}", "agent": agent_name}
    except Exception as exc:
        return {"error": str(exc), "agent": agent_name}


async def run_orchestrator(
    disaster_id: int,
    command: str,
    db: AsyncSession,
) -> Dict[str, Any]:
    """
    Orchestrate a full relief operation response.

    Flow
    ----
    1. Fetch disaster context from DB.
    2. Ask Groq which sub-agents to call and in what order.
    3. Split agents_to_call into a parallel batch and a sequential tail.
    4. Run parallel batch with asyncio.gather(return_exceptions=True).
    5. Run sequential agents one-by-one.
    6. Aggregate all results and log.
    7. Return comprehensive response dict.
    """
    start_ts = time.time()

    try:
        # ── 1. Fetch disaster context ──────────────────────────────────────
        disaster = await crud.get_disaster(db, disaster_id)
        if disaster is None:
            return {
                "error": f"Disaster {disaster_id} not found.",
                "agent": _AGENT_NAME,
            }

        supplies = await crud.list_supplies_by_disaster(db, disaster_id)
        critical_supplies = await crud.get_critical_supplies(db, disaster_id)
        today_shifts = await crud.list_shifts_by_disaster(db, disaster_id)
        active_tasks = await crud.list_tasks(db, disaster_id, status_filter="pending")
        overdue_tasks = await crud.get_overdue_tasks(db, disaster_id)

        # ── 2. Build orchestrator prompt ───────────────────────────────────
        user_message = (
            f"DISASTER: id={disaster.id} name={disaster.name!r} "
            f"type={disaster.type} location={disaster.location!r} "
            f"severity={disaster.severity}/5 status={disaster.status}\n\n"
            f"CURRENT STATE:\n"
            f"  supplies_total={len(supplies)} "
            f"critical_supplies={len(critical_supplies)}\n"
            f"  shifts_today={len(today_shifts)}\n"
            f"  pending_tasks={len(active_tasks)} "
            f"overdue_tasks={len(overdue_tasks)}\n\n"
            f"COMMAND: {command}"
        )

        # ── 3. Call Groq for orchestration plan ────────────────────────────
        plan = await _call_groq(_SYSTEM_PROMPT, user_message)

    except Exception as exc:
        duration_ms = int((time.time() - start_ts) * 1000)
        await crud.create_log(
            db,
            agent_name=_AGENT_NAME,
            action="run_orchestrator",
            input_summary=f"disaster_id={disaster_id} command={command[:120]}",
            output_summary=f"ERROR: {exc}",
            status="error",
            duration_ms=duration_ms,
            disaster_id=disaster_id,
        )
        await db.commit()
        return {"error": str(exc), "agent": _AGENT_NAME}

    agents_to_call: List[str] = plan.get("agents_to_call", [])

    # ── 4. Split into parallel / sequential groups ─────────────────────────
    parallel_agents = [a for a in agents_to_call if a in _PARALLEL_SAFE]
    sequential_agents = [a for a in agents_to_call if a in _SEQUENTIAL_AFTER]
    # Any agent not in either set runs sequentially as a fallback
    unknown_agents = [
        a for a in agents_to_call
        if a not in _PARALLEL_SAFE and a not in _SEQUENTIAL_AFTER
    ]

    results: Dict[str, Any] = {}
    agents_called: List[str] = []

    # ── 5a. Run parallel batch ─────────────────────────────────────────────
    if parallel_agents:
        parallel_coros = [
            _invoke_agent(name, disaster_id, command, db)
            for name in parallel_agents
        ]
        parallel_results = await asyncio.gather(
            *parallel_coros, return_exceptions=True
        )
        for name, res in zip(parallel_agents, parallel_results):
            agents_called.append(name)
            if isinstance(res, BaseException):
                results[name] = {"error": str(res), "agent": name}
            else:
                results[name] = res

        # Brief pause after parallel batch to avoid Groq rate limits
        if sequential_agents or unknown_agents:
            await asyncio.sleep(2)

    # ── 5b. Run sequential agents ──────────────────────────────────────────
    for name in sequential_agents + unknown_agents:
        agents_called.append(name)
        results[name] = await _invoke_agent(name, disaster_id, command, db)

    duration_ms = int((time.time() - start_ts) * 1000)

    # ── 6. Log ─────────────────────────────────────────────────────────────
    errors_count = sum(1 for r in results.values() if "error" in r)
    await crud.create_log(
        db,
        agent_name=_AGENT_NAME,
        action="run_orchestrator",
        input_summary=f"disaster_id={disaster_id} command={command[:120]}",
        output_summary=(
            f"agents_called={agents_called} "
            f"errors={errors_count} "
            f"summary={plan.get('orchestrator_summary', '')[:120]}"
        ),
        status="success" if errors_count == 0 else "error",
        duration_ms=duration_ms,
        disaster_id=disaster_id,
    )
    await db.commit()

    # ── 7. Return aggregated response ──────────────────────────────────────
    return {
        "orchestrator_summary": plan.get("orchestrator_summary", ""),
        "agents_called": agents_called,
        "results": results,
        "alerts": plan.get("alerts", []),
        "next_recommended_actions": plan.get("next_recommended_actions", []),
        "execution_time_ms": duration_ms,
    }