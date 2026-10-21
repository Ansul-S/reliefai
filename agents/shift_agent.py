"""
Shift Coverage Agent
Detects gaps in 24/7 coverage, attempts AI-driven gap-filling by matching
volunteers to open slots, and persists new shifts to the database.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict, List

from sqlalchemy.ext.asyncio import AsyncSession

import database.crud as crud
from database.models import SEVERITY_SCALE_MAX
from agents._groq_client import _call_groq

_AGENT_NAME = "shift_agent"

# Built from the same constants the gap scan uses. The prompt used to say
# "minimum 2 volunteers per 6-hour block per zone" while the scan worked in
# 4-hour blocks and the schema has no zones, so the model was asked to plan
# against a rule the data never followed.
_SYSTEM_PROMPT = (
    "You are Shift Coverage Agent for disaster relief. "
    f"Ensure 24/7 coverage with minimum {crud.SHIFT_GAP_MIN_VOLUNTEERS} "
    f"volunteers per {crud.SHIFT_GAP_BLOCK_HOURS}-hour block. "
    "When gaps found: find best replacement (matching skills, not over "
    f"{crud.MAX_VOLUNTEER_HOURS_PER_DAY}hr today, available at that time). "
    "If no replacement available, escalate immediately. "
    "Track: Medical role needs medical/first_aid skills. "
    "Logistics needs driving/logistics. "
    "General needs any available volunteer. "
    "You must respond with ONLY valid JSON: "
    "{gaps_found: list[dict], gaps_filled: list[dict], "
    "escalations_needed: list[dict], volunteers_contacted: list[str], "
    "coverage_status: str}. "
    "Each gap_filled dict must contain: volunteer_id (int), role (str), "
    "start_time (ISO-8601 str), end_time (ISO-8601 str), "
    "gap_date (YYYY-MM-DD str), gap_hour_block (int). "
    "No explanation, no markdown, no code blocks."
)


def _volunteer_line(v: Any, hours_today: float) -> str:
    return (
        f"  id={v.id} name={v.name!r} skills={v.skills} "
        f"languages={v.languages} hours_today={hours_today} "
        f"availability={v.availability}"
    )


async def run_shift_agent(disaster_id: int, db: AsyncSession) -> Dict[str, Any]:
    """
    Run full gap-detection → gap-filling cycle for *disaster_id*.

    Steps
    -----
    1. Detect shift gaps via crud.detect_shift_gaps.
    2. Load available volunteers + today's hours.
    3. Ask Groq for a gap-filling plan.
    4. For each recommended gap_filled, create actual Shift rows in DB.
    5. Log and return.
    """
    start_ts = time.time()

    try:
        # ── 1. Gather context ──────────────────────────────────────────────
        disaster = await crud.get_disaster(db, disaster_id)
        if disaster is None:
            return {"error": f"Disaster {disaster_id} not found.", "agent": _AGENT_NAME}

        gaps = await crud.detect_shift_gaps(db, disaster_id)
        available_vols = await crud.get_available_volunteers(db, disaster_id)

        # Build hours-today map
        hours_map: Dict[int, float] = {}
        for v in available_vols:
            hours_map[v.id] = await crud.get_volunteer_hours_today(db, v.id)

        # Filter volunteers who still have capacity today
        max_hours = crud.MAX_VOLUNTEER_HOURS_PER_DAY
        eligible_vols = [
            v for v in available_vols if hours_map.get(v.id, 0) < max_hours
        ]

        vol_lines = "\n".join(
            _volunteer_line(v, hours_map.get(v.id, 0.0)) for v in eligible_vols
        )

        gap_lines = "\n".join(
            f"  date={g['date']} hour_block={g['hour_block']:02d}:00 "
            f"current_volunteers={g['volunteers_count']} "
            f"(need>={crud.SHIFT_GAP_MIN_VOLUNTEERS})"
            for g in gaps[:30]  # cap context length
        )

        # ── 2. Build context ───────────────────────────────────────────────
        context = (
            f"DISASTER: id={disaster.id} name={disaster.name!r} "
            f"location={disaster.location!r} "
            f"severity={disaster.severity}/{SEVERITY_SCALE_MAX}\n\n"
            f"SHIFT GAPS DETECTED ({len(gaps)} total, showing first 30):\n"
            + (gap_lines or "  (no gaps — coverage looks good)")
            + f"\n\nELIGIBLE VOLUNTEERS ({len(eligible_vols)} with "
            f"<{max_hours} hrs today):\n"
            + (vol_lines or "  (none available)")
            + "\n\nFor each gap, recommend a volunteer assignment. "
            "Use ISO-8601 datetimes with UTC timezone for start_time and end_time. "
            f"The filled shift should cover the {crud.SHIFT_GAP_BLOCK_HOURS}-hour "
            "block exactly. "
            "If no eligible volunteer can fill a gap, add it to escalations_needed."
        )

        # ── 3. Call Groq ───────────────────────────────────────────────────
        result = await _call_groq(_SYSTEM_PROMPT, context)

    except Exception as exc:
        duration_ms = int((time.time() - start_ts) * 1000)
        await crud.create_log(
            db,
            agent_name=_AGENT_NAME,
            action="run_shift_agent",
            input_summary=f"disaster_id={disaster_id}",
            output_summary=f"ERROR: {exc}",
            status="error",
            duration_ms=duration_ms,
            disaster_id=disaster_id,
        )
        await db.commit()
        return {"error": str(exc), "agent": _AGENT_NAME}

    duration_ms = int((time.time() - start_ts) * 1000)

    # ── 4. Create recommended shifts in DB ────────────────────────────────
    # The model's gaps_filled is a proposal. Only fills naming a volunteer
    # from the eligible list it was shown, with a sane time range, are
    # written; every other one is recorded with its reason instead of being
    # dropped silently, so a coordinator can see what was not booked.
    eligible_ids = {v.id for v in eligible_vols}
    shifts_created: List[Dict[str, Any]] = []
    fills_rejected: List[Dict[str, Any]] = []
    for fill in result.get("gaps_filled", []):
        try:
            volunteer_id = int(fill["volunteer_id"])
            if volunteer_id not in eligible_ids:
                raise ValueError(
                    f"volunteer {volunteer_id} was not among the eligible volunteers"
                )
            role = str(fill.get("role", "general"))
            start_raw = fill.get("start_time", "")
            end_raw = fill.get("end_time", "")

            start_dt = datetime.fromisoformat(start_raw)
            end_dt = datetime.fromisoformat(end_raw)

            # Ensure timezone-aware
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            if end_dt <= start_dt:
                raise ValueError("end_time must be after start_time")

            new_shift = await crud.create_shift(
                db,
                disaster_id=disaster_id,
                volunteer_id=volunteer_id,
                role=role,
                start_time=start_dt,
                end_time=end_dt,
                notes="Auto-scheduled by shift_agent to fill coverage gap.",
            )
            shifts_created.append(
                {
                    "shift_id": new_shift.id,
                    "volunteer_id": volunteer_id,
                    "role": role,
                    "start_time": new_shift.start_time.isoformat(),
                    "end_time": new_shift.end_time.isoformat(),
                }
            )
        except Exception as exc:  # noqa: BLE001 - recorded, never raised
            fills_rejected.append({"fill": fill, "reason": str(exc)})

    # Always present, so a consumer reads what was written rather than falling
    # back to the model's own claim in gaps_filled.
    result["shifts_created_in_db"] = shifts_created
    result["fills_rejected"] = fills_rejected
    result["coverage_rules"] = {
        "block_hours": crud.SHIFT_GAP_BLOCK_HOURS,
        "min_volunteers": crud.SHIFT_GAP_MIN_VOLUNTEERS,
        "max_hours_per_day": max_hours,
    }

    # ── 5. Log ─────────────────────────────────────────────────────────────
    await crud.create_log(
        db,
        agent_name=_AGENT_NAME,
        action="run_shift_agent",
        input_summary=f"disaster_id={disaster_id} gaps_detected={len(gaps)}",
        output_summary=(
            f"gaps_filled={len(result.get('gaps_filled', []))} "
            f"shifts_in_db={len(shifts_created)} "
            f"rejected={len(fills_rejected)} "
            f"escalations={len(result.get('escalations_needed', []))} "
            f"status={result.get('coverage_status', '')[:80]}"
        ),
        status="success",
        duration_ms=duration_ms,
        disaster_id=disaster_id,
    )
    await db.commit()

    result["agent"] = _AGENT_NAME
    result["execution_time_ms"] = duration_ms
    return result