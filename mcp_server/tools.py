"""
ReliefAI MCP Server — FastMCP tool definitions.

All tools open their own database session so they can be called independently
of any FastAPI request context.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastmcp import FastMCP

from database import AsyncSessionLocal
from database.models import SEVERITY_SCALE_MAX
import database.crud as crud
from mcp_server.weather import get_forecast

# ---------------------------------------------------------------------------
# FastMCP app
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="ReliefAI",
    instructions=(
        "Tools for orchestrating post-disaster relief operations. "
        "Manage shifts, tasks, supplies, notes, and broadcasts."
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 datetime string to a timezone-aware datetime."""
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _shift_to_dict(shift: Any, include_volunteer_name: bool = True) -> Dict[str, Any]:
    d: Dict[str, Any] = {
        "id": shift.id,
        "disaster_id": shift.disaster_id,
        "volunteer_id": shift.volunteer_id,
        "role": shift.role,
        "start_time": shift.start_time.isoformat() if shift.start_time else None,
        "end_time": shift.end_time.isoformat() if shift.end_time else None,
        "status": shift.status,
        "notes": shift.notes,
        "created_at": shift.created_at.isoformat() if shift.created_at else None,
    }
    if include_volunteer_name and hasattr(shift, "volunteer") and shift.volunteer:
        d["volunteer_name"] = shift.volunteer.name
    return d


def _task_to_dict(task: Any) -> Dict[str, Any]:
    return {
        "id": task.id,
        "disaster_id": task.disaster_id,
        "title": task.title,
        "description": task.description,
        "assigned_to": task.assigned_to,
        "agent_assigned": task.agent_assigned,
        "priority": task.priority,
        "status": task.status,
        "due_date": task.due_date.isoformat() if task.due_date else None,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
    }


def _supply_to_dict(supply: Any, critical: bool = False) -> Dict[str, Any]:
    return {
        "id": supply.id,
        "disaster_id": supply.disaster_id,
        "item_name": supply.item_name,
        "category": supply.category,
        "quantity_needed": supply.quantity_needed,
        "quantity_available": supply.quantity_available,
        "unit": supply.unit,
        "priority": supply.priority,
        "status": supply.status,
        "updated_at": supply.updated_at.isoformat() if supply.updated_at else None,
        "is_critical": critical,
        "critical_threshold_pct": round(crud.CRITICAL_SUPPLY_THRESHOLD * 100),
    }


def _broadcast_to_dict(broadcast: Any) -> Dict[str, Any]:
    return {
        "id": broadcast.id,
        "disaster_id": broadcast.disaster_id,
        "message_en": broadcast.message_en,
        "message_translations": broadcast.message_translations,
        "channel": broadcast.channel,
        "target_audience": broadcast.target_audience,
        "sent_at": broadcast.sent_at.isoformat() if broadcast.sent_at else None,
        "status": broadcast.status,
    }


def _volunteer_to_dict(v: Any, hours_today: float = 0.0) -> Dict[str, Any]:
    return {
        "id": v.id,
        "name": v.name,
        "phone": v.phone,
        "email": v.email,
        "skills": v.skills or [],
        "languages": v.languages or [],
        "availability": v.availability or {},
        "is_active": v.is_active,
        "hours_today": hours_today,
    }


def _note_to_dict(note: Any) -> Dict[str, Any]:
    return {
        "id": note.id,
        "disaster_id": note.disaster_id,
        "content": note.content,
        "tags": note.tags,
        "created_by": note.created_by,
        "created_at": note.created_at.isoformat() if note.created_at else None,
    }


# ---------------------------------------------------------------------------
# Calendar / Shift Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def schedule_shift(
    disaster_id: int,
    volunteer_id: int,
    role: str,
    start_time: str,
    end_time: str,
) -> Dict[str, Any]:
    """Create a new shift for a volunteer on a disaster response."""
    start_dt = _parse_dt(start_time)
    end_dt = _parse_dt(end_time)

    if start_dt is None or end_dt is None:
        return {"error": "Invalid start_time or end_time. Use ISO-8601 format."}
    if end_dt <= start_dt:
        return {"error": "end_time must be after start_time."}

    try:
        async with AsyncSessionLocal() as db:
            # The ids come from the model. Refuse one that names nothing, in
            # words it can act on, rather than store a shift for nobody.
            if await crud.get_disaster(db, disaster_id) is None:
                return {"error": f"Disaster {disaster_id} not found."}
            if await crud.get_volunteer(db, volunteer_id) is None:
                return {"error": f"Volunteer {volunteer_id} not found."}

            shift = await crud.create_shift(
                db=db,
                disaster_id=disaster_id,
                volunteer_id=volunteer_id,
                role=role,
                start_time=start_dt,
                end_time=end_dt,
            )
            await db.commit()
            # Reload with relationships
            shift = await crud.get_shift(db, shift.id)
            return _shift_to_dict(shift)
    except Exception as exc:
        return {"error": str(exc)}


@mcp.tool()
async def get_shifts(
    disaster_id: int,
    date_filter: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List all shifts for a disaster, optionally filtered by date (YYYY-MM-DD)."""
    date_obj = None
    if date_filter:
        try:
            from datetime import date as date_type
            date_obj = date_type.fromisoformat(date_filter)
        except ValueError:
            return [{"error": f"Invalid date_filter '{date_filter}'. Use YYYY-MM-DD."}]

    try:
        async with AsyncSessionLocal() as db:
            shifts = await crud.list_shifts_by_disaster(db, disaster_id, date_obj)
            return [_shift_to_dict(s) for s in shifts]
    except Exception as exc:
        return [{"error": str(exc)}]


@mcp.tool()
async def cancel_shift(shift_id: int, reason: str = "") -> Dict[str, Any]:
    """Cancel a scheduled shift and record the reason."""
    try:
        async with AsyncSessionLocal() as db:
            shift = await crud.cancel_shift(db, shift_id, reason or None)
            if shift is None:
                return {"error": f"Shift {shift_id} not found."}
            await db.commit()
            return {
                "success": True,
                "shift_id": shift_id,
                "status": shift.status,
                "message": f"Shift {shift_id} cancelled successfully.",
            }
    except Exception as exc:
        return {"error": str(exc)}


@mcp.tool()
async def detect_shift_gaps(disaster_id: int) -> Dict[str, Any]:
    """
    Detect blocks in the coming days where too few volunteers are scheduled.

    Returns the scan parameters alongside the gaps themselves::

        {"window_days": 7, "block_hours": 4, "min_volunteers": 2,
         "gap_count": 12, "gaps": [{date, hour_block, volunteers_count}, ...]}

    Quote `window_days` when describing the period examined — say "the next 7
    days" only because the payload says 7, never from memory. The window is
    configuration and can change.
    """
    try:
        async with AsyncSessionLocal() as db:
            gaps = await crud.detect_shift_gaps(db, disaster_id)
            return {
                "window_days": crud.SHIFT_GAP_WINDOW_DAYS,
                "block_hours": crud.SHIFT_GAP_BLOCK_HOURS,
                "min_volunteers": crud.SHIFT_GAP_MIN_VOLUNTEERS,
                "gap_count": len(gaps),
                "gaps": gaps,
            }
    except Exception as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Task Manager Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def create_task(
    disaster_id: int,
    title: str,
    description: str,
    assigned_to: str = "",
    priority: str = "medium",
    due_date: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new task for a disaster response operation."""
    due_dt = _parse_dt(due_date)

    try:
        async with AsyncSessionLocal() as db:
            task = await crud.create_task(
                db=db,
                disaster_id=disaster_id,
                title=title,
                description=description,
                assigned_to=assigned_to or None,
                priority=priority,
                due_date=due_dt,
            )
            await db.commit()
            return _task_to_dict(task)
    except Exception as exc:
        return {"error": str(exc)}


@mcp.tool()
async def update_task(
    task_id: int,
    status: str,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    """Update the status of a task, optionally appending notes."""
    valid_statuses = {"pending", "in_progress", "done", "blocked"}
    if status not in valid_statuses:
        return {"error": f"Invalid status '{status}'. Must be one of {valid_statuses}."}

    try:
        async with AsyncSessionLocal() as db:
            task = await crud.update_task_status(db, task_id, status, notes)
            if task is None:
                return {"error": f"Task {task_id} not found."}
            await db.commit()
            return _task_to_dict(task)
    except Exception as exc:
        return {"error": str(exc)}


@mcp.tool()
async def list_tasks(
    disaster_id: int,
    status_filter: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List tasks for a disaster, optionally filtered by status."""
    try:
        async with AsyncSessionLocal() as db:
            tasks = await crud.list_tasks(db, disaster_id, status_filter)
            return [_task_to_dict(t) for t in tasks]
    except Exception as exc:
        return [{"error": str(exc)}]


@mcp.tool()
async def get_overdue_tasks(disaster_id: int) -> List[Dict[str, Any]]:
    """Return all tasks that are past their due date and not yet completed."""
    try:
        async with AsyncSessionLocal() as db:
            tasks = await crud.get_overdue_tasks(db, disaster_id)
            return [_task_to_dict(t) for t in tasks]
    except Exception as exc:
        return [{"error": str(exc)}]


# ---------------------------------------------------------------------------
# Notes / Intel Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def add_note(
    disaster_id: int,
    content: str,
    tags: Optional[List[str]] = None,
    created_by: str = "system",
) -> Dict[str, Any]:
    """Add an intelligence note to a disaster response."""
    try:
        async with AsyncSessionLocal() as db:
            note = await crud.create_note(
                db=db,
                disaster_id=disaster_id,
                content=content,
                created_by=created_by,
                tags=tags or [],
            )
            await db.commit()
            return _note_to_dict(note)
    except Exception as exc:
        return {"error": str(exc)}


@mcp.tool()
async def search_notes(disaster_id: int, query: str) -> List[Dict[str, Any]]:
    """Search notes for a disaster by keyword (content or tags)."""
    try:
        async with AsyncSessionLocal() as db:
            notes = await crud.search_notes(db, disaster_id, query)
            return [_note_to_dict(n) for n in notes]
    except Exception as exc:
        return [{"error": str(exc)}]


@mcp.tool()
async def get_supply_status(disaster_id: int) -> List[Dict[str, Any]]:
    """
    Return all supplies for a disaster with an `is_critical` flag set to True
    for items below the critical threshold. Each record carries the threshold
    that classified it as `critical_threshold_pct`; cite that figure rather
    than a remembered one when explaining why an item is critical.
    """
    try:
        async with AsyncSessionLocal() as db:
            all_supplies = await crud.list_supplies_by_disaster(db, disaster_id)
            critical_ids = {
                s.id
                for s in await crud.get_critical_supplies(db, disaster_id)
            }
            return [
                _supply_to_dict(s, critical=(s.id in critical_ids))
                for s in all_supplies
            ]
    except Exception as exc:
        return [{"error": str(exc)}]


@mcp.tool()
async def update_supply(
    supply_id: int,
    quantity_available: float,
    status: str,
) -> Dict[str, Any]:
    """Update the available quantity and status of a supply item."""
    valid_statuses = {"needed", "procuring", "available", "depleted"}
    if status not in valid_statuses:
        return {
            "error": f"Invalid status '{status}'. Must be one of {valid_statuses}."
        }

    try:
        async with AsyncSessionLocal() as db:
            supply = await crud.update_supply(
                db,
                supply_id,
                quantity_available=quantity_available,
                status=status,
            )
            if supply is None:
                return {"error": f"Supply {supply_id} not found."}
            await db.commit()
            return _supply_to_dict(supply)
    except Exception as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Broadcast Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def draft_broadcast(
    disaster_id: int,
    message: str,
    target_audience: str,
    channel: str = "all",
) -> Dict[str, Any]:
    """
    Draft a new broadcast message for a disaster. Channel options:
    sms / email / whatsapp / all.
    """
    valid_channels = {"sms", "email", "whatsapp", "all"}
    if channel not in valid_channels:
        return {
            "error": f"Invalid channel '{channel}'. Must be one of {valid_channels}."
        }

    try:
        async with AsyncSessionLocal() as db:
            broadcast = await crud.create_broadcast(
                db=db,
                disaster_id=disaster_id,
                message_en=message,
                channel=channel,
                target_audience=target_audience,
                status="draft",
            )
            await db.commit()
            return _broadcast_to_dict(broadcast)
    except Exception as exc:
        return {"error": str(exc)}


@mcp.tool()
async def send_broadcast(broadcast_id: int) -> Dict[str, Any]:
    """Mark a draft broadcast as sent and record the sent timestamp."""
    try:
        async with AsyncSessionLocal() as db:
            broadcast = await crud.get_broadcast(db, broadcast_id)
            if broadcast is None:
                return {"error": f"Broadcast {broadcast_id} not found."}
            if broadcast.status == "sent":
                return {
                    "error": f"Broadcast {broadcast_id} has already been sent.",
                    "sent_at": broadcast.sent_at.isoformat() if broadcast.sent_at else None,
                }
            broadcast = await crud.update_broadcast(
                db,
                broadcast_id,
                status="sent",
                sent_at=datetime.now(tz=timezone.utc),
            )
            await db.commit()
            return {
                "success": True,
                "broadcast_id": broadcast_id,
                "status": broadcast.status,
                "sent_at": broadcast.sent_at.isoformat() if broadcast.sent_at else None,
            }
    except Exception as exc:
        return {"error": str(exc)}


@mcp.tool()
async def get_broadcasts(disaster_id: int) -> List[Dict[str, Any]]:
    """List all broadcasts for a disaster, newest first."""
    try:
        async with AsyncSessionLocal() as db:
            broadcasts = await crud.list_broadcasts(db, disaster_id)
            return [_broadcast_to_dict(b) for b in broadcasts]
    except Exception as exc:
        return [{"error": str(exc)}]


# ---------------------------------------------------------------------------
# Situation / Disaster Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_disaster_status(disaster_id: int) -> Dict[str, Any]:
    """Get an overview of a disaster: name, type, location, severity, status, and
    counts of supplies, critical supplies, shifts, pending tasks and overdue tasks.
    `severity` is reported on a scale whose upper bound is `severity_scale_max`,
    so write "severity 4/5" only from those two fields.
    Call this first when you need general situational awareness."""
    try:
        async with AsyncSessionLocal() as db:
            disaster = await crud.get_disaster(db, disaster_id)
            if disaster is None:
                return {"error": f"Disaster {disaster_id} not found."}

            supplies = await crud.list_supplies_by_disaster(db, disaster_id)
            critical = await crud.get_critical_supplies(db, disaster_id)
            shifts = await crud.list_shifts_by_disaster(db, disaster_id)
            pending = await crud.list_tasks(db, disaster_id, status_filter="pending")
            overdue = await crud.get_overdue_tasks(db, disaster_id)

            return {
                "id": disaster.id,
                "name": disaster.name,
                "type": disaster.type,
                "location": disaster.location,
                "severity": disaster.severity,
                "severity_scale_max": SEVERITY_SCALE_MAX,
                "status": disaster.status,
                "counts": {
                    "supplies_total": len(supplies),
                    "critical_supplies": len(critical),
                    "shifts": len(shifts),
                    "pending_tasks": len(pending),
                    "overdue_tasks": len(overdue),
                },
                "critical_supply_names": [s.item_name for s in critical],
            }
    except Exception as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Volunteer Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_volunteers(disaster_id: int) -> List[Dict[str, Any]]:
    """List volunteers assigned to a disaster, including their skills, languages
    and hours already worked today."""
    try:
        async with AsyncSessionLocal() as db:
            volunteers = await crud.list_volunteers_by_disaster(db, disaster_id)
            out: List[Dict[str, Any]] = []
            for v in volunteers:
                hours = await crud.get_volunteer_hours_today(db, v.id)
                out.append(_volunteer_to_dict(v, hours_today=hours))
            return out
    except Exception as exc:
        return [{"error": str(exc)}]


@mcp.tool()
async def find_available_volunteers(
    disaster_id: int,
    required_skills: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Find active volunteers available for assignment, optionally filtered to those
    who have at least one of the given skills (e.g. ["medical", "logistics"]).
    Use this before scheduling a shift to pick a suitable volunteer."""
    try:
        async with AsyncSessionLocal() as db:
            volunteers = await crud.get_available_volunteers(
                db, disaster_id, required_skills or None
            )
            out: List[Dict[str, Any]] = []
            for v in volunteers:
                hours = await crud.get_volunteer_hours_today(db, v.id)
                out.append(_volunteer_to_dict(v, hours_today=hours))
            return out
    except Exception as exc:
        return [{"error": str(exc)}]


# ---------------------------------------------------------------------------
# Higher-order reasoning tools
#
# These wrap the specialist agents that own genuine LLM domain logic
# (multilingual translation, narrative report writing). They are imported
# lazily to avoid a circular import: agents/__init__ imports the orchestrator,
# which imports this module.
# ---------------------------------------------------------------------------


@mcp.tool()
async def translate_and_send_broadcast(
    disaster_id: int,
    message: str,
    languages: Optional[List[str]] = None,
    channel: str = "all",
) -> Dict[str, Any]:
    """Translate an emergency message into multiple languages, enforce SMS length
    limits, and save it as a broadcast. Supported language codes include
    en, hi, ta, id, tl, ja. Use this for any public or community announcement."""
    try:
        from agents.comms_agent import run_comms_agent

        async with AsyncSessionLocal() as db:
            result = await run_comms_agent(
                disaster_id,
                message=message,
                languages=languages or ["en", "hi", "ta", "id", "tl", "ja"],
                channel=channel,
                db=db,
            )
            await db.commit()
            return result
    except Exception as exc:
        return {"error": str(exc)}


@mcp.tool()
async def generate_situation_report(
    disaster_id: int,
    report_type: str = "internal",
) -> Dict[str, Any]:
    """Generate a written situation report aggregating supplies, shifts, volunteers
    and tasks. report_type must be one of: donor, authority, internal."""
    if report_type not in {"donor", "authority", "internal"}:
        return {
            "error": f"Invalid report_type {report_type!r}. "
            "Use donor, authority or internal."
        }
    try:
        from agents.report_agent import run_report_agent

        async with AsyncSessionLocal() as db:
            result = await run_report_agent(disaster_id, report_type, db)
            await db.commit()
            return result
    except Exception as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# External conditions
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_weather_forecast(disaster_id: int) -> Dict[str, Any]:
    """Get the 24-hour weather forecast for a disaster's location.

    Use this when weather could affect the response — outdoor shifts, shelter
    capacity, tarpaulin or blanket demand, road access, or the timing of a
    distribution. Returns total expected rainfall, peak wind, when rain starts,
    and the next six hours hour by hour.

    This is the only tool that reads from outside the operation database, so it
    can be unavailable. When "available" is false, tell the user no forecast
    could be retrieved rather than estimating conditions yourself.
    """
    try:
        async with AsyncSessionLocal() as db:
            disaster = await crud.get_disaster(db, disaster_id)
            if not disaster:
                return {"error": f"Disaster {disaster_id} not found."}
            location = disaster.location
    except Exception as exc:
        return {"error": str(exc)}

    forecast = await get_forecast(location)
    return {"disaster_id": disaster_id, **forecast}


# ---------------------------------------------------------------------------
# Entry point (run as standalone MCP server)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import asyncio
    from database import create_all_tables

    async def _startup() -> None:
        await create_all_tables()

    asyncio.run(_startup())
    mcp.run()