from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database.models import (
    AgentLog,
    Broadcast,
    Disaster,
    Note,
    Shift,
    Supply,
    Task,
    Volunteer,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# Disaster
# ---------------------------------------------------------------------------


async def create_disaster(
    db: AsyncSession,
    name: str,
    type: str,
    location: str,
    severity: int,
    status: str = "active",
) -> Disaster:
    disaster = Disaster(
        name=name,
        type=type,
        location=location,
        severity=severity,
        status=status,
    )
    db.add(disaster)
    await db.flush()
    await db.refresh(disaster)
    return disaster


async def get_disaster(db: AsyncSession, disaster_id: int) -> Optional[Disaster]:
    result = await db.execute(
        select(Disaster).where(Disaster.id == disaster_id)
    )
    return result.scalar_one_or_none()


async def list_disasters(db: AsyncSession) -> List[Disaster]:
    result = await db.execute(select(Disaster).order_by(Disaster.created_at.desc()))
    return list(result.scalars().all())


async def update_disaster_status(
    db: AsyncSession, disaster_id: int, status: str
) -> Optional[Disaster]:
    disaster = await get_disaster(db, disaster_id)
    if disaster is None:
        return None
    disaster.status = status
    disaster.updated_at = _utcnow()
    await db.flush()
    await db.refresh(disaster)
    return disaster


async def get_active_disasters(db: AsyncSession) -> List[Disaster]:
    result = await db.execute(
        select(Disaster)
        .where(Disaster.status == "active")
        .order_by(Disaster.severity.desc())
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Volunteer
# ---------------------------------------------------------------------------


async def create_volunteer(
    db: AsyncSession,
    name: str,
    phone: str,
    email: str,
    skills: Optional[List[str]] = None,
    languages: Optional[List[str]] = None,
    availability: Optional[Dict[str, Any]] = None,
) -> Volunteer:
    volunteer = Volunteer(
        name=name,
        phone=phone,
        email=email,
        skills=skills or [],
        languages=languages or [],
        availability=availability or {},
    )
    db.add(volunteer)
    await db.flush()
    await db.refresh(volunteer)
    return volunteer


async def get_volunteer(db: AsyncSession, volunteer_id: int) -> Optional[Volunteer]:
    result = await db.execute(
        select(Volunteer).where(Volunteer.id == volunteer_id)
    )
    return result.scalar_one_or_none()


async def list_volunteers(db: AsyncSession, active_only: bool = False) -> List[Volunteer]:
    stmt = select(Volunteer)
    if active_only:
        stmt = stmt.where(Volunteer.is_active == True)  # noqa: E712
    stmt = stmt.order_by(Volunteer.name)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def list_volunteers_by_disaster(
    db: AsyncSession, disaster_id: int
) -> List[Volunteer]:
    """Return distinct volunteers that have at least one shift for this disaster."""
    result = await db.execute(
        select(Volunteer)
        .join(Shift, Shift.volunteer_id == Volunteer.id)
        .where(
            and_(
                Shift.disaster_id == disaster_id,
                Shift.status.notin_(["cancelled"]),
            )
        )
        .distinct()
        .order_by(Volunteer.name)
    )
    return list(result.scalars().all())


async def update_volunteer(
    db: AsyncSession,
    volunteer_id: int,
    **kwargs: Any,
) -> Optional[Volunteer]:
    volunteer = await get_volunteer(db, volunteer_id)
    if volunteer is None:
        return None
    allowed = {
        "name", "phone", "email", "skills", "languages", "availability", "is_active"
    }
    for key, value in kwargs.items():
        if key in allowed:
            setattr(volunteer, key, value)
    await db.flush()
    await db.refresh(volunteer)
    return volunteer


async def get_available_volunteers(
    db: AsyncSession,
    disaster_id: int,
    required_skills: Optional[List[str]] = None,
) -> List[Volunteer]:
    """Return active volunteers. If required_skills provided, filter by overlap."""
    volunteers = await list_volunteers(db, active_only=True)
    if not required_skills:
        return volunteers

    matched: List[Volunteer] = []
    for v in volunteers:
        volunteer_skills: List[str] = v.skills or []
        if any(s in volunteer_skills for s in required_skills):
            matched.append(v)
    return matched


# ---------------------------------------------------------------------------
# Shift
# ---------------------------------------------------------------------------


async def create_shift(
    db: AsyncSession,
    disaster_id: int,
    volunteer_id: int,
    role: str,
    start_time: datetime,
    end_time: datetime,
    notes: Optional[str] = None,
) -> Shift:
    shift = Shift(
        disaster_id=disaster_id,
        volunteer_id=volunteer_id,
        role=role,
        start_time=start_time,
        end_time=end_time,
        notes=notes,
    )
    db.add(shift)
    await db.flush()
    await db.refresh(shift)
    return shift


async def get_shift(db: AsyncSession, shift_id: int) -> Optional[Shift]:
    result = await db.execute(
        select(Shift)
        .options(selectinload(Shift.volunteer), selectinload(Shift.disaster))
        .where(Shift.id == shift_id)
    )
    return result.scalar_one_or_none()


async def list_shifts_by_disaster(
    db: AsyncSession,
    disaster_id: int,
    date_filter: Optional[date] = None,
) -> List[Shift]:
    stmt = (
        select(Shift)
        .options(selectinload(Shift.volunteer))
        .where(Shift.disaster_id == disaster_id)
    )
    if date_filter is not None:
        day_start = datetime.combine(date_filter, datetime.min.time()).replace(
            tzinfo=timezone.utc
        )
        day_end = day_start + timedelta(days=1)
        stmt = stmt.where(
            and_(Shift.start_time >= day_start, Shift.start_time < day_end)
        )
    stmt = stmt.order_by(Shift.start_time)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def cancel_shift(
    db: AsyncSession, shift_id: int, reason: Optional[str] = None
) -> Optional[Shift]:
    shift = await get_shift(db, shift_id)
    if shift is None:
        return None
    shift.status = "cancelled"
    if reason:
        shift.notes = (shift.notes or "") + f" [Cancelled: {reason}]"
    await db.flush()
    await db.refresh(shift)
    return shift


async def complete_shift(db: AsyncSession, shift_id: int) -> Optional[Shift]:
    shift = await get_shift(db, shift_id)
    if shift is None:
        return None
    shift.status = "completed"
    await db.flush()
    await db.refresh(shift)
    return shift


async def detect_shift_gaps(
    db: AsyncSession, disaster_id: int
) -> List[Dict[str, Any]]:
    """
    For the next 7 days, examine every 4-hour block (0,4,8,12,16,20) and
    return blocks where fewer than 2 non-cancelled shifts overlap.
    Returns list of {date, hour_block, volunteers_count}.
    """
    now = _utcnow()
    gaps: List[Dict[str, Any]] = []

    for day_offset in range(7):
        check_date = (now + timedelta(days=day_offset)).date()
        for hour_block in range(0, 24, 4):
            block_start = datetime(
                check_date.year,
                check_date.month,
                check_date.day,
                hour_block,
                tzinfo=timezone.utc,
            )
            block_end = block_start + timedelta(hours=4)

            result = await db.execute(
                select(func.count(Shift.id)).where(
                    and_(
                        Shift.disaster_id == disaster_id,
                        Shift.status.notin_(["cancelled"]),
                        Shift.start_time < block_end,
                        Shift.end_time > block_start,
                    )
                )
            )
            count = result.scalar_one()
            if count < 2:
                gaps.append(
                    {
                        "date": check_date.isoformat(),
                        "hour_block": hour_block,
                        "volunteers_count": count,
                    }
                )

    return gaps


async def get_volunteer_hours_today(
    db: AsyncSession, volunteer_id: int
) -> float:
    """Return total scheduled/confirmed hours for a volunteer today (UTC)."""
    today = _utcnow().date()
    day_start = datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)

    result = await db.execute(
        select(Shift).where(
            and_(
                Shift.volunteer_id == volunteer_id,
                Shift.status.in_(["scheduled", "confirmed"]),
                Shift.start_time >= day_start,
                Shift.start_time < day_end,
            )
        )
    )
    shifts = result.scalars().all()
    total_hours = 0.0
    for s in shifts:
        delta = s.end_time - s.start_time
        total_hours += delta.total_seconds() / 3600
    return round(total_hours, 2)


# ---------------------------------------------------------------------------
# Supply
# ---------------------------------------------------------------------------


async def create_supply(
    db: AsyncSession,
    disaster_id: int,
    item_name: str,
    category: str,
    quantity_needed: float,
    quantity_available: float,
    unit: str,
    priority: str = "medium",
    status: str = "needed",
) -> Supply:
    supply = Supply(
        disaster_id=disaster_id,
        item_name=item_name,
        category=category,
        quantity_needed=quantity_needed,
        quantity_available=quantity_available,
        unit=unit,
        priority=priority,
        status=status,
    )
    db.add(supply)
    await db.flush()
    await db.refresh(supply)
    return supply


async def get_supply(db: AsyncSession, supply_id: int) -> Optional[Supply]:
    result = await db.execute(select(Supply).where(Supply.id == supply_id))
    return result.scalar_one_or_none()


async def list_supplies_by_disaster(
    db: AsyncSession, disaster_id: int
) -> List[Supply]:
    result = await db.execute(
        select(Supply)
        .where(Supply.disaster_id == disaster_id)
        .order_by(Supply.priority.desc(), Supply.item_name)
    )
    return list(result.scalars().all())


async def update_supply(
    db: AsyncSession,
    supply_id: int,
    **kwargs: Any,
) -> Optional[Supply]:
    supply = await get_supply(db, supply_id)
    if supply is None:
        return None
    allowed = {
        "item_name", "category", "quantity_needed", "quantity_available",
        "unit", "priority", "status",
    }
    for key, value in kwargs.items():
        if key in allowed:
            setattr(supply, key, value)
    supply.updated_at = _utcnow()
    await db.flush()
    await db.refresh(supply)
    return supply


async def get_critical_supplies(
    db: AsyncSession, disaster_id: int, threshold: float = 0.3
) -> List[Supply]:
    """Return supplies where quantity_available / quantity_needed < threshold."""
    all_supplies = await list_supplies_by_disaster(db, disaster_id)
    critical: List[Supply] = []
    for s in all_supplies:
        if s.quantity_needed > 0:
            ratio = s.quantity_available / s.quantity_needed
            if ratio < threshold:
                critical.append(s)
        else:
            # needed=0 means we don't track it as critical
            pass
    return critical


# ---------------------------------------------------------------------------
# Task
# ---------------------------------------------------------------------------


async def create_task(
    db: AsyncSession,
    disaster_id: int,
    title: str,
    description: str,
    assigned_to: Optional[str] = None,
    agent_assigned: Optional[str] = None,
    priority: str = "medium",
    due_date: Optional[datetime] = None,
) -> Task:
    task = Task(
        disaster_id=disaster_id,
        title=title,
        description=description,
        assigned_to=assigned_to,
        agent_assigned=agent_assigned,
        priority=priority,
        due_date=due_date,
    )
    db.add(task)
    await db.flush()
    await db.refresh(task)
    return task


async def get_task(db: AsyncSession, task_id: int) -> Optional[Task]:
    result = await db.execute(select(Task).where(Task.id == task_id))
    return result.scalar_one_or_none()


async def list_tasks(
    db: AsyncSession,
    disaster_id: int,
    status_filter: Optional[str] = None,
) -> List[Task]:
    stmt = select(Task).where(Task.disaster_id == disaster_id)
    if status_filter:
        stmt = stmt.where(Task.status == status_filter)
    stmt = stmt.order_by(Task.created_at.desc())
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def update_task_status(
    db: AsyncSession,
    task_id: int,
    status: str,
    notes: Optional[str] = None,
) -> Optional[Task]:
    task = await get_task(db, task_id)
    if task is None:
        return None
    task.status = status
    if status == "done":
        task.completed_at = _utcnow()
    if notes:
        task.description = task.description + f"\n[Update]: {notes}"
    await db.flush()
    await db.refresh(task)
    return task


async def get_overdue_tasks(db: AsyncSession, disaster_id: int) -> List[Task]:
    now = _utcnow()
    result = await db.execute(
        select(Task).where(
            and_(
                Task.disaster_id == disaster_id,
                Task.status.notin_(["done", "blocked"]),
                Task.due_date < now,
                Task.due_date.isnot(None),
            )
        )
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Broadcast
# ---------------------------------------------------------------------------


async def create_broadcast(
    db: AsyncSession,
    disaster_id: int,
    message_en: str,
    channel: str,
    target_audience: str,
    message_translations: Optional[Dict[str, str]] = None,
    status: str = "draft",
) -> Broadcast:
    broadcast = Broadcast(
        disaster_id=disaster_id,
        message_en=message_en,
        channel=channel,
        target_audience=target_audience,
        message_translations=message_translations or {},
        status=status,
    )
    db.add(broadcast)
    await db.flush()
    await db.refresh(broadcast)
    return broadcast


async def get_broadcast(db: AsyncSession, broadcast_id: int) -> Optional[Broadcast]:
    result = await db.execute(
        select(Broadcast).where(Broadcast.id == broadcast_id)
    )
    return result.scalar_one_or_none()


async def list_broadcasts(db: AsyncSession, disaster_id: int) -> List[Broadcast]:
    result = await db.execute(
        select(Broadcast)
        .where(Broadcast.disaster_id == disaster_id)
        .order_by(Broadcast.id.desc())
    )
    return list(result.scalars().all())


async def update_broadcast(
    db: AsyncSession, broadcast_id: int, **kwargs: Any
) -> Optional[Broadcast]:
    broadcast = await get_broadcast(db, broadcast_id)
    if broadcast is None:
        return None
    allowed = {
        "message_en", "message_translations", "channel",
        "target_audience", "sent_at", "status",
    }
    for key, value in kwargs.items():
        if key in allowed:
            setattr(broadcast, key, value)
    await db.flush()
    await db.refresh(broadcast)
    return broadcast


# ---------------------------------------------------------------------------
# AgentLog
# ---------------------------------------------------------------------------


async def create_log(
    db: AsyncSession,
    agent_name: str,
    action: str,
    input_summary: str,
    output_summary: str,
    status: str,
    duration_ms: int,
    disaster_id: Optional[int] = None,
) -> AgentLog:
    log = AgentLog(
        disaster_id=disaster_id,
        agent_name=agent_name,
        action=action,
        input_summary=input_summary,
        output_summary=output_summary,
        status=status,
        duration_ms=duration_ms,
    )
    db.add(log)
    await db.flush()
    await db.refresh(log)
    return log


async def list_logs(
    db: AsyncSession, disaster_id: Optional[int] = None, limit: int = 50
) -> List[AgentLog]:
    stmt = select(AgentLog)
    if disaster_id is not None:
        stmt = stmt.where(AgentLog.disaster_id == disaster_id)
    stmt = stmt.order_by(AgentLog.created_at.desc()).limit(limit)
    result = await db.execute(stmt)
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Note
# ---------------------------------------------------------------------------


async def create_note(
    db: AsyncSession,
    disaster_id: int,
    content: str,
    created_by: str,
    tags: Optional[List[str]] = None,
) -> Note:
    note = Note(
        disaster_id=disaster_id,
        content=content,
        created_by=created_by,
        tags=tags or [],
    )
    db.add(note)
    await db.flush()
    await db.refresh(note)
    return note


async def list_notes(db: AsyncSession, disaster_id: int) -> List[Note]:
    result = await db.execute(
        select(Note)
        .where(Note.disaster_id == disaster_id)
        .order_by(Note.created_at.desc())
    )
    return list(result.scalars().all())


async def search_notes(
    db: AsyncSession, disaster_id: int, query: str
) -> List[Note]:
    """Simple case-insensitive substring search on content and tags."""
    all_notes = await list_notes(db, disaster_id)
    q = query.lower()
    matched: List[Note] = []
    for note in all_notes:
        if q in note.content.lower():
            matched.append(note)
            continue
        if any(q in str(tag).lower() for tag in (note.tags or [])):
            matched.append(note)
    return matched