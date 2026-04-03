"""
Volunteer Management Agent
Handles scheduling, assignment, replacement, and coverage analysis.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict

from sqlalchemy.ext.asyncio import AsyncSession

import database.crud as crud
from agents._groq_client import _call_groq

_AGENT_NAME = "volunteer_agent"

_SYSTEM_PROMPT = (
    "You are Volunteer Management Agent for disaster relief. "
    "You manage volunteer scheduling and assignments. "
    "Rules: never assign more than 12 hours per day to one volunteer. "
    "Prioritize language skills for community-facing roles. "
    "Match technical skills to roles (medical→triage, driving→logistics, "
    "construction→shelter). "
    "When finding replacements, check availability and current shift load. "
    "You must respond with ONLY valid JSON: "
    "{action_taken: str, volunteers_affected: list[str], "
    "shifts_modified: list[dict], warnings: list[str], "
    "recommendations: list[str]}. "
    "No explanation, no markdown, no code blocks."
)


def _fmt_volunteer(v: Any) -> str:
    return (
        f"  - id={v.id} name={v.name!r} phone={v.phone} "
        f"skills={v.skills} languages={v.languages} "
        f"active={v.is_active}"
    )


def _fmt_shift(s: Any) -> str:
    vol_name = s.volunteer.name if hasattr(s, "volunteer") and s.volunteer else "?"
    return (
        f"  - id={s.id} volunteer={vol_name!r}(id={s.volunteer_id}) "
        f"role={s.role!r} "
        f"start={s.start_time.isoformat() if s.start_time else '?'} "
        f"end={s.end_time.isoformat() if s.end_time else '?'} "
        f"status={s.status}"
    )


async def run_volunteer_agent(
    disaster_id: int, task: str, db: AsyncSession
) -> Dict[str, Any]:
    """
    Analyse volunteer coverage for *disaster_id* and carry out *task*.

    Steps
    -----
    1. Load volunteers, today/tomorrow shifts, and shift gaps from DB.
    2. Build a rich context string.
    3. Call Groq for an action plan.
    4. Log the run.
    5. Return parsed result dict.
    """
    start_ts = time.time()

    try:
        # ── 1. Gather context ──────────────────────────────────────────────
        disaster = await crud.get_disaster(db, disaster_id)
        if disaster is None:
            return {"error": f"Disaster {disaster_id} not found.", "agent": _AGENT_NAME}

        volunteers = await crud.list_volunteers_by_disaster(db, disaster_id)
        all_active_vols = await crud.list_volunteers(db, active_only=True)

        today = datetime.now(tz=timezone.utc).date()
        tomorrow = today + timedelta(days=1)

        today_shifts = await crud.list_shifts_by_disaster(db, disaster_id, today)
        tomorrow_shifts = await crud.list_shifts_by_disaster(db, disaster_id, tomorrow)
        gaps = await crud.detect_shift_gaps(db, disaster_id)

        # Hours today per active volunteer
        hours_lines: list[str] = []
        for v in all_active_vols:
            h = await crud.get_volunteer_hours_today(db, v.id)
            hours_lines.append(f"  - volunteer_id={v.id} name={v.name!r} hours_today={h}")

        # ── 2. Build context ───────────────────────────────────────────────
        context = (
            f"DISASTER: id={disaster.id} name={disaster.name!r} "
            f"location={disaster.location!r} severity={disaster.severity}/5\n\n"
            f"VOLUNTEERS ASSIGNED TO THIS DISASTER ({len(volunteers)}):\n"
            + "\n".join(_fmt_volunteer(v) for v in volunteers)
            + f"\n\nALL ACTIVE VOLUNTEERS ({len(all_active_vols)}):\n"
            + "\n".join(_fmt_volunteer(v) for v in all_active_vols)
            + f"\n\nVOLUNTEER HOURS TODAY:\n"
            + "\n".join(hours_lines)
            + f"\n\nSHIFTS TODAY ({len(today_shifts)}):\n"
            + ("\n".join(_fmt_shift(s) for s in today_shifts) or "  (none)")
            + f"\n\nSHIFTS TOMORROW ({len(tomorrow_shifts)}):\n"
            + ("\n".join(_fmt_shift(s) for s in tomorrow_shifts) or "  (none)")
            + f"\n\nSHIFT GAPS (next 7 days, <2 volunteers per 4-hr block) — {len(gaps)} gaps:\n"
            + (
                "\n".join(
                    f"  - {g['date']} hour_block={g['hour_block']:02d}:00 "
                    f"volunteers={g['volunteers_count']}"
                    for g in gaps[:20]  # cap to avoid token overload
                )
                or "  (none)"
            )
            + "\n\nTASK: "
            + task
        )

        # ── 3. Call Groq ───────────────────────────────────────────────────
        result = await _call_groq(_SYSTEM_PROMPT, context)

    except Exception as exc:
        duration_ms = int((time.time() - start_ts) * 1000)
        await crud.create_log(
            db,
            agent_name=_AGENT_NAME,
            action="run_volunteer_agent",
            input_summary=f"disaster_id={disaster_id} task={task[:120]}",
            output_summary=f"ERROR: {exc}",
            status="error",
            duration_ms=duration_ms,
            disaster_id=disaster_id,
        )
        await db.commit()
        return {"error": str(exc), "agent": _AGENT_NAME}

    duration_ms = int((time.time() - start_ts) * 1000)

    # ── 4. Log ─────────────────────────────────────────────────────────────
    await crud.create_log(
        db,
        agent_name=_AGENT_NAME,
        action="run_volunteer_agent",
        input_summary=f"disaster_id={disaster_id} task={task[:120]}",
        output_summary=(
            f"action={result.get('action_taken', '')[:120]} "
            f"warnings={len(result.get('warnings', []))}"
        ),
        status="success",
        duration_ms=duration_ms,
        disaster_id=disaster_id,
    )
    await db.commit()

    result["agent"] = _AGENT_NAME
    result["execution_time_ms"] = duration_ms
    return result