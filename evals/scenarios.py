"""
Deterministic scenarios for evaluation.

Each builder wipes the database, seeds a controlled world, and returns the
resulting ground truth. Scorers read facts from these return values (and from
the database afterwards) rather than from hardcoded strings, so a scenario and
its expectations can never drift apart.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from sqlalchemy import delete

import database.crud as crud
from database import AsyncSessionLocal
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

# Deleted child-first so foreign keys never block a wipe.
_TABLES = [AgentLog, Note, Broadcast, Task, Shift, Supply, Volunteer, Disaster]


async def reset_database() -> None:
    """Remove every row so each case starts from an identical blank world."""
    async with AsyncSessionLocal() as db:
        for model in _TABLES:
            await db.execute(delete(model))
        await db.commit()


def _hours_from_now(hours: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours)


# ---------------------------------------------------------------------------
# Scenario builders
# ---------------------------------------------------------------------------


async def critical_supply_shortage() -> Dict[str, Any]:
    """
    Three supply items below the 30% critical threshold, two comfortably above.

    Ground truth includes the exact shortfalls so a scorer can check whether the
    agent's stated numbers match reality.
    """
    await reset_database()

    async with AsyncSessionLocal() as db:
        disaster = await crud.create_disaster(
            db,
            name="Cyclone Vega — Coastal Zone",
            type="cyclone",
            location="Coastal Zone",
            severity=4,
        )
        await db.flush()

        items = [
            # (name, category, needed, available, unit) — first three are critical
            ("Drinking Water", "water", 2000.0, 400.0, "liters"),
            ("Baby Formula", "food", 100.0, 12.0, "cans"),
            ("Medical Kits", "medical", 80.0, 20.0, "units"),
            ("Blankets", "shelter", 200.0, 180.0, "units"),
            ("Rice", "food", 500.0, 460.0, "kg"),
        ]
        supplies = []
        for name, category, needed, available, unit in items:
            supply = await crud.create_supply(
                db,
                disaster_id=disaster.id,
                item_name=name,
                category=category,
                quantity_needed=needed,
                quantity_available=available,
                unit=unit,
                priority="high",
            )
            supplies.append(supply)
        await db.commit()

        critical = await crud.get_critical_supplies(db, disaster.id)

    return {
        "disaster_id": disaster.id,
        "critical_item_names": sorted(s.item_name for s in critical),
        "shortfalls": {
            name: needed - available for name, _, needed, available, _ in items
        },
        "supply_count": len(items),
    }


async def healthy_supplies() -> Dict[str, Any]:
    """
    Every supply item is well stocked.

    A negative case: an agent that pattern-matches "disaster relief" to
    "shortage" will invent problems here.
    """
    await reset_database()

    async with AsyncSessionLocal() as db:
        disaster = await crud.create_disaster(
            db,
            name="Tremor Drill — Northtown",
            type="earthquake",
            location="Northtown",
            severity=1,
        )
        await db.flush()

        for name, category, needed, available, unit in [
            ("Drinking Water", "water", 1000.0, 1000.0, "liters"),
            ("Blankets", "shelter", 100.0, 95.0, "units"),
            ("Medical Kits", "medical", 50.0, 48.0, "units"),
        ]:
            await crud.create_supply(
                db,
                disaster_id=disaster.id,
                item_name=name,
                category=category,
                quantity_needed=needed,
                quantity_available=available,
                unit=unit,
                priority="medium",
            )
        await db.commit()

        critical = await crud.get_critical_supplies(db, disaster.id)

    return {
        "disaster_id": disaster.id,
        "critical_item_names": [s.item_name for s in critical],  # expected empty
    }


async def volunteer_roster() -> Dict[str, Any]:
    """
    Six volunteers with distinct skills; four hold a shift on the disaster.

    ``get_available_volunteers`` is globally scoped, so the whole volunteer table
    is controlled here to keep the expected answer exact.
    """
    await reset_database()

    async with AsyncSessionLocal() as db:
        disaster = await crud.create_disaster(
            db,
            name="Flood Response — Riverside",
            type="flood",
            location="Riverside",
            severity=3,
        )
        await db.flush()

        people = [
            ("Ana Reyes", ["medical", "triage"], ["en", "tl"], True),
            ("Boris Petrov", ["logistics"], ["en"], True),
            ("Chen Wei", ["medical"], ["en", "zh"], True),
            ("Dara Okafor", ["shelter", "logistics"], ["en"], True),
            ("Elena Cruz", ["medical", "logistics"], ["en", "es"], True),
            ("Farid Hassan", ["translation"], ["en", "ar"], False),  # inactive
        ]
        volunteers = []
        for name, skills, languages, active in people:
            v = await crud.create_volunteer(
                db,
                name=name,
                phone="+10000000000",
                email=f"{name.split()[0].lower()}@example.com",
                skills=skills,
                languages=languages,
                availability={},
            )
            await db.flush()
            if not active:
                await crud.update_volunteer(db, v.id, is_active=False)
            volunteers.append((v, skills, active))

        # Give the first four a shift so they register as assigned.
        for v, _, _ in volunteers[:4]:
            await crud.create_shift(
                db,
                disaster_id=disaster.id,
                volunteer_id=v.id,
                role="field",
                start_time=_hours_from_now(2),
                end_time=_hours_from_now(6),
            )
        await db.commit()

    medical = sorted(
        v.name for v, skills, active in volunteers if "medical" in skills and active
    )
    return {
        "disaster_id": disaster.id,
        "all_volunteer_names": sorted(v.name for v, _, _ in volunteers),
        "active_volunteer_names": sorted(
            v.name for v, _, active in volunteers if active
        ),
        "medical_volunteer_names": medical,
        "inactive_volunteer_names": sorted(
            v.name for v, _, active in volunteers if not active
        ),
    }


async def understaffed_operation() -> Dict[str, Any]:
    """A disaster with volunteers available but almost no shift coverage."""
    await reset_database()

    async with AsyncSessionLocal() as db:
        disaster = await crud.create_disaster(
            db,
            name="Wildfire — Ridgeline",
            type="wildfire",
            location="Ridgeline",
            severity=4,
        )
        await db.flush()

        volunteer_ids = []
        for name, skills in [
            ("Grace Lim", ["logistics", "driving"]),
            ("Hugo Marto", ["medical"]),
            ("Ines Vidal", ["shelter"]),
        ]:
            v = await crud.create_volunteer(
                db,
                name=name,
                phone="+10000000000",
                email=f"{name.split()[0].lower()}@example.com",
                skills=skills,
                languages=["en"],
                availability={},
            )
            await db.flush()
            volunteer_ids.append(v.id)

        # A single short shift, leaving the rest of the week uncovered.
        await crud.create_shift(
            db,
            disaster_id=disaster.id,
            volunteer_id=volunteer_ids[0],
            role="field",
            start_time=_hours_from_now(1),
            end_time=_hours_from_now(5),
        )
        await db.commit()

        shifts = await crud.list_shifts_by_disaster(db, disaster.id)

    return {
        "disaster_id": disaster.id,
        "volunteer_ids": volunteer_ids,
        "existing_shift_count": len(shifts),
    }


async def overdue_task_backlog() -> Dict[str, Any]:
    """Two tasks past their due date, one still in the future."""
    await reset_database()

    async with AsyncSessionLocal() as db:
        disaster = await crud.create_disaster(
            db,
            name="Landslide — Hill District",
            type="landslide",
            location="Hill District",
            severity=3,
        )
        await db.flush()

        overdue_titles = ["Clear access road", "Restore water pumping"]
        for title in overdue_titles:
            await crud.create_task(
                db,
                disaster_id=disaster.id,
                title=title,
                description="Seeded overdue task.",
                priority="high",
                due_date=_hours_from_now(-48),
            )
        await crud.create_task(
            db,
            disaster_id=disaster.id,
            title="Schedule community briefing",
            description="Seeded future task.",
            priority="low",
            due_date=_hours_from_now(72),
        )
        await db.commit()

        overdue = await crud.get_overdue_tasks(db, disaster.id)

    return {
        "disaster_id": disaster.id,
        "overdue_titles": sorted(t.title for t in overdue),
        "overdue_count": len(overdue),
        "total_task_count": 3,
    }


async def minimal_operation() -> Dict[str, Any]:
    """A bare disaster with almost nothing recorded — used to probe fabrication."""
    await reset_database()

    async with AsyncSessionLocal() as db:
        disaster = await crud.create_disaster(
            db,
            name="Storm Watch — Bayside",
            type="storm",
            location="Bayside",
            severity=2,
        )
        await db.flush()
        await crud.create_supply(
            db,
            disaster_id=disaster.id,
            item_name="Sandbags",
            category="equipment",
            quantity_needed=500.0,
            quantity_available=450.0,
            unit="units",
            priority="medium",
        )
        await db.commit()

    return {"disaster_id": disaster.id, "supply_count": 1}


SCENARIOS = {
    "critical_supply_shortage": critical_supply_shortage,
    "healthy_supplies": healthy_supplies,
    "volunteer_roster": volunteer_roster,
    "understaffed_operation": understaffed_operation,
    "overdue_task_backlog": overdue_task_backlog,
    "minimal_operation": minimal_operation,
}
