"""
Status Report Agent
Generates rich Markdown reports for three audiences: DONOR, AUTHORITY, INTERNAL.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict

from sqlalchemy.ext.asyncio import AsyncSession

import database.crud as crud
from agents._groq_client import _call_groq

_AGENT_NAME = "report_agent"

_SYSTEM_PROMPT = (
    "You are Status Report Agent for disaster relief. "
    "Generate structured reports for three audiences. "
    "DONOR report: focus on lives helped, outcomes, supplies delivered, volunteer hours. "
    "AUTHORITY report: focus on operational gaps, resource needs, critical risks, "
    "immediate actions required. "
    "INTERNAL report: full operational detail, all metrics, agent activity, "
    "recommendations. "
    "Always prominently flag CRITICAL items. "
    "Use clear section headers. "
    "You must respond with ONLY valid JSON: "
    "{report_type: str, generated_at: str, "
    "sections: {executive_summary: str, volunteer_status: str, "
    "supply_status: str, active_tasks: str, recent_broadcasts: str, "
    "critical_flags: str, recommendations: str}, "
    "critical_flags: list[str], markdown_report: str}. "
    "No explanation, no markdown wrapping, no code blocks."
)

_VALID_REPORT_TYPES = {"donor", "authority", "internal"}


def _fmt_task(t: Any) -> str:
    due = t.due_date.isoformat() if t.due_date else "no due date"
    return (
        f"  id={t.id} [{t.priority.upper()}] {t.title!r} "
        f"status={t.status} assigned={t.assigned_to or 'unassigned'} due={due}"
    )


def _fmt_supply(s: Any) -> str:
    pct = (
        round(s.quantity_available / s.quantity_needed * 100, 1)
        if s.quantity_needed > 0
        else 0.0
    )
    return (
        f"  id={s.id} {s.item_name!r} cat={s.category} "
        f"avail={s.quantity_available}/{s.quantity_needed}{s.unit} ({pct}%) "
        f"status={s.status}"
    )


def _fmt_broadcast(b: Any) -> str:
    sent = b.sent_at.isoformat() if b.sent_at else "not sent"
    return (
        f"  id={b.id} channel={b.channel} target={b.target_audience!r} "
        f"status={b.status} sent={sent}\n"
        f"    msg_en={b.message_en[:120]!r}"
    )


def _fmt_log(lg: Any) -> str:
    return (
        f"  [{lg.created_at.isoformat() if lg.created_at else '?'}] "
        f"agent={lg.agent_name} action={lg.action} "
        f"status={lg.status} duration_ms={lg.duration_ms}"
    )


async def run_report_agent(
    disaster_id: int, report_type: str, db: AsyncSession
) -> Dict[str, Any]:
    """
    Build a comprehensive context from the DB and ask Groq to produce a
    structured report for the requested *report_type*.
    """
    start_ts = time.time()

    safe_type = report_type.lower().strip()
    if safe_type not in _VALID_REPORT_TYPES:
        safe_type = "internal"

    try:
        # ── 1. Gather comprehensive context ───────────────────────────────
        disaster = await crud.get_disaster(db, disaster_id)
        if disaster is None:
            return {"error": f"Disaster {disaster_id} not found.", "agent": _AGENT_NAME}

        # Volunteers
        volunteers = await crud.list_volunteers_by_disaster(db, disaster_id)
        all_active = await crud.list_volunteers(db, active_only=True)

        # Shifts (today)
        today = datetime.now(tz=timezone.utc).date()
        today_shifts = await crud.list_shifts_by_disaster(db, disaster_id, today)

        # Hours per volunteer
        total_vol_hours = 0.0
        for v in all_active:
            total_vol_hours += await crud.get_volunteer_hours_today(db, v.id)

        # Supplies
        all_supplies = await crud.list_supplies_by_disaster(db, disaster_id)
        critical_supplies = await crud.get_critical_supplies(db, disaster_id)
        critical_ids = {s.id for s in critical_supplies}

        # Tasks
        all_tasks = await crud.list_tasks(db, disaster_id)
        open_tasks = [t for t in all_tasks if t.status not in ("done",)]
        overdue_tasks = await crud.get_overdue_tasks(db, disaster_id)

        # Broadcasts
        broadcasts = await crud.list_broadcasts(db, disaster_id)
        recent_broadcasts = broadcasts[:5]

        # Agent logs
        logs = await crud.list_logs(db, disaster_id, limit=20)

        # Notes
        notes = await crud.list_notes(db, disaster_id)

        # Gaps
        gaps = await crud.detect_shift_gaps(db, disaster_id)
        critical_gaps = [g for g in gaps if g["volunteers_count"] == 0]

        # ── 2. Build context string ────────────────────────────────────────
        supply_lines = "\n".join(
            f"{_fmt_supply(s)} {'⚠CRITICAL' if s.id in critical_ids else ''}"
            for s in all_supplies
        )

        context = (
            f"DISASTER OVERVIEW\n"
            f"  id={disaster.id} name={disaster.name!r} type={disaster.type} "
            f"location={disaster.location!r} severity={disaster.severity}/5 "
            f"status={disaster.status}\n"
            f"  created={disaster.created_at.isoformat() if disaster.created_at else '?'}\n\n"
            f"VOLUNTEER SUMMARY\n"
            f"  total_assigned_to_disaster={len(volunteers)}\n"
            f"  total_active_in_system={len(all_active)}\n"
            f"  shifts_today={len(today_shifts)}\n"
            f"  total_volunteer_hours_today={total_vol_hours:.1f}\n"
            f"  coverage_gaps_next_7days={len(gaps)} "
            f"(critical_zero_coverage={len(critical_gaps)})\n\n"
            f"SUPPLY STATUS ({len(all_supplies)} items, "
            f"{len(critical_supplies)} critical)\n"
            + (supply_lines or "  (none)")
            + f"\n\nTASKS ({len(all_tasks)} total, "
            f"{len(open_tasks)} open, {len(overdue_tasks)} OVERDUE)\n"
            + "OPEN TASKS:\n"
            + ("\n".join(_fmt_task(t) for t in open_tasks[:15]) or "  (none)")
            + "\nOVERDUE TASKS:\n"
            + ("\n".join(_fmt_task(t) for t in overdue_tasks[:10]) or "  (none)")
            + f"\n\nRECENT BROADCASTS (last {len(recent_broadcasts)}):\n"
            + ("\n".join(_fmt_broadcast(b) for b in recent_broadcasts) or "  (none)")
            + f"\n\nRECENT AGENT ACTIVITY (last {len(logs)} logs):\n"
            + ("\n".join(_fmt_log(lg) for lg in logs) or "  (none)")
            + f"\n\nINTEL NOTES ({len(notes)} total):\n"
            + (
                "\n".join(
                    f"  [{n.created_at.isoformat() if n.created_at else '?'}] "
                    f"by={n.created_by!r} tags={n.tags}: {n.content[:200]}"
                    for n in notes[:10]
                )
                or "  (none)"
            )
            + f"\n\nREPORT TYPE REQUESTED: {safe_type.upper()}\n"
            f"GENERATED AT: {datetime.now(tz=timezone.utc).isoformat()}\n\n"
            "Generate the full structured report now."
        )

        # ── 3. Call Groq ───────────────────────────────────────────────────
        result = await _call_groq(_SYSTEM_PROMPT, context)

    except Exception as exc:
        duration_ms = int((time.time() - start_ts) * 1000)
        await crud.create_log(
            db,
            agent_name=_AGENT_NAME,
            action="run_report_agent",
            input_summary=f"disaster_id={disaster_id} report_type={safe_type}",
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
        action="run_report_agent",
        input_summary=f"disaster_id={disaster_id} report_type={safe_type}",
        output_summary=(
            f"critical_flags={len(result.get('critical_flags', []))} "
            f"sections={list(result.get('sections', {}).keys())}"
        ),
        status="success",
        duration_ms=duration_ms,
        disaster_id=disaster_id,
    )
    await db.commit()

    result["agent"] = _AGENT_NAME
    result["execution_time_ms"] = duration_ms
    return result