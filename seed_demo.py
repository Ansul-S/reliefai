"""
seed_demo.py — ReliefAI Demo Seeder
====================================
Idempotent: safe to run multiple times.
Checks if "Typhoon Carina" disaster exists before inserting.
After seeding, runs 5 orchestrator commands and prints results.

Usage:
    python seed_demo.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

# ── must set DATABASE_URL before importing db layer ────────────────
os.environ.setdefault(
    "DATABASE_URL", "sqlite+aiosqlite:///./relief.db"
)

from database import create_all_tables, AsyncSessionLocal
import database.crud as crud
from agents.orchestrator import run_orchestrator

# ── Constants ──────────────────────────────────────────────────────
DISASTER_NAME = "Typhoon Carina — Metro Manila, Philippines"

ALL_AVAILABILITY = {
    "monday": True, "tuesday": True, "wednesday": True,
    "thursday": True, "friday": True, "saturday": True, "sunday": True,
}


# ══════════════════════════════════════════════════════════════════════
# SEED FUNCTION
# ══════════════════════════════════════════════════════════════════════

async def seed(db) -> int:
    """
    Idempotent seed. Returns disaster_id (existing or newly created).
    """

    # ── Check if already seeded ────────────────────────────────────
    existing = await crud.list_disasters(db)
    for d in existing:
        if d.name == DISASTER_NAME:
            print(f"  ✓ Disaster already exists (id={d.id}) — skipping creation.")
            return d.id

    print("  Creating disaster...")
    disaster = await crud.create_disaster(
        db,
        name=DISASTER_NAME,
        type="typhoon",
        location="Metro Manila, Philippines",
        severity=4,
        status="active",
    )
    did = disaster.id
    print(f"  ✓ Disaster created: id={did}")

    # ── Volunteers ─────────────────────────────────────────────────
    print("  Creating 12 volunteers...")
    vol_data = [
        ("Maria Santos",   "+63-917-001-0001", "maria.santos@reliefai.demo",
         ["first_aid", "evacuation", "community_outreach"],          ["Filipino", "English"]),
        ("Jose Reyes",     "+63-917-001-0002", "jose.reyes@reliefai.demo",
         ["logistics", "driving", "heavy_equipment"],                ["Filipino", "English"]),
        ("Priya Kumar",    "+91-985-001-0001", "priya.kumar@reliefai.demo",
         ["medical", "triage", "trauma_care"],                       ["English", "Tamil", "Hindi"]),
        ("Ahmad Malik",    "+62-812-001-0001", "ahmad.malik@reliefai.demo",
         ["construction", "shelter_setup", "carpentry"],             ["Bahasa Indonesia", "English"]),
        ("Lina Tanaka",    "+81-090-001-0001", "lina.tanaka@reliefai.demo",
         ["communications", "translation", "admin"],                 ["Japanese", "English", "Filipino"]),
        ("Carlos Mendoza", "+63-917-001-0003", "carlos.mendoza@reliefai.demo",
         ["search_rescue", "first_aid", "swimming"],                 ["Filipino", "English"]),
        ("Aisha Rahman",   "+62-812-001-0002", "aisha.rahman@reliefai.demo",
         ["food_distribution", "nutrition", "cooking"],              ["Bahasa Indonesia", "English"]),
        ("Ravi Sharma",    "+91-985-001-0002", "ravi.sharma@reliefai.demo",
         ["water_supply", "sanitation", "engineering"],              ["Hindi", "English"]),
        ("Sofia Cruz",     "+63-917-001-0004", "sofia.cruz@reliefai.demo",
         ["counseling", "mental_health", "community_outreach"],      ["Filipino", "English"]),
        ("Ben Tan",        "+65-900-001-0001", "ben.tan@reliefai.demo",
         ["logistics", "inventory", "driving"],                      ["English"]),
        ("Nadia Santos",   "+63-917-001-0005", "nadia.santos@reliefai.demo",
         ["medical", "nursing", "first_aid"],                        ["Filipino", "English", "Tamil"]),
        ("Marco Reyes",    "+63-917-001-0006", "marco.reyes@reliefai.demo",
         ["shelter_setup", "construction", "general"],               ["Filipino", "English"]),
    ]

    volunteer_ids: dict[str, int] = {}
    for name, phone, email, skills, languages in vol_data:
        v = await crud.create_volunteer(
            db,
            name=name,
            phone=phone,
            email=email,
            skills=skills,
            languages=languages,
            availability=ALL_AVAILABILITY,
        )
        volunteer_ids[name] = v.id

    print(f"  ✓ 12 volunteers created.")

    # ── Supplies ───────────────────────────────────────────────────
    print("  Creating 8 supplies...")
    supplies_data = [
        # (item_name, category, needed, available, unit, priority, status)
        ("Rice",             "food",      500.0,  120.0,  "kg",    "high",   "needed"),
        ("Bottled Water",    "water",    2000.0,  500.0,  "liters","high",   "needed"),
        ("Baby Food",        "food",      100.0,   10.0,  "units", "high",   "needed"),
        ("Medical Kits",     "medical",    50.0,   48.0,  "kits",  "high",   "available"),
        ("Emergency Tents",  "shelter",   100.0,   30.0,  "units", "high",   "needed"),
        ("Generators",       "equipment",  10.0,    3.0,  "units", "medium", "needed"),
        ("Blankets",         "shelter",   200.0,  180.0,  "units", "medium", "available"),
        ("Sanitation Kits",  "medical",   150.0,   90.0,  "kits",  "medium", "procuring"),
    ]
    for item_name, category, needed, available, unit, priority, status in supplies_data:
        await crud.create_supply(
            db,
            disaster_id=did,
            item_name=item_name,
            category=category,
            quantity_needed=needed,
            quantity_available=available,
            unit=unit,
            priority=priority,
            status=status,
        )
    print("  ✓ 8 supplies created.")

    # ── Shifts ─────────────────────────────────────────────────────
    print("  Creating 6 shifts for today...")
    today = datetime.now(tz=timezone.utc)

    def ts(hour: int) -> datetime:
        """Return today's date at *hour* UTC."""
        return today.replace(hour=hour, minute=0, second=0, microsecond=0)

    shifts_config = [
        # (volunteer_name, role, start_hour, end_hour, status, notes)
        ("Maria Santos",   "Medical Coordinator",  6,  12, "confirmed", None),
        ("Priya Kumar",    "Triage Lead",           6,  12, "confirmed", None),
        ("Jose Reyes",     "Logistics Coordinator", 12, 18, "cancelled", "volunteer sick"),
        ("Carlos Mendoza", "Search and Rescue",     12, 18, "cancelled", "family emergency"),
        ("Nadia Santos",   "Medical Support",       18,  0, "cancelled", "transport issue"),
        ("Ben Tan",        "Supply Distribution",   18,  0, "confirmed", None),
    ]

    for vol_name, role, start_h, end_h, status, notes in shifts_config:
        vol_id = volunteer_ids[vol_name]
        start_dt = ts(start_h)
        # midnight = next day
        end_dt = ts(end_h) if end_h != 0 else (today + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        shift = await crud.create_shift(
            db,
            disaster_id=did,
            volunteer_id=vol_id,
            role=role,
            start_time=start_dt,
            end_time=end_dt,
            notes=notes,
        )
        # Apply status
        if status == "cancelled":
            await crud.cancel_shift(db, shift.id, notes or "Cancelled at seed")
        elif status == "completed":
            await crud.complete_shift(db, shift.id)
        # "confirmed" → keep as scheduled (update directly)
        elif status == "confirmed":
            shift.status = "confirmed"

    print("  ✓ 6 shifts created.")

    # ── Notes ──────────────────────────────────────────────────────
    print("  Creating 3 intel notes...")
    notes_data = [
        (
            "Water supply trucks delayed due to road damage on C5 highway. "
            "Expect 6-hour delay in water delivery.",
            ["water", "logistics", "urgent"],
            "Field Coordinator",
        ),
        (
            "Rizal High School evacuation center now open. Capacity 500 persons. "
            "Currently housing 347 displaced residents.",
            ["evacuation", "capacity", "update"],
            "Site Manager",
        ),
        (
            "Weather update: Typhoon has passed. Clear skies expected for next 48 hours. "
            "Transitioning to recovery phase.",
            ["weather", "recovery", "update"],
            "Weather Monitor",
        ),
    ]
    for content, tags, created_by in notes_data:
        await crud.create_note(db, did, content, created_by, tags)
    print("  ✓ 3 notes created.")

    await db.commit()
    return did


# ══════════════════════════════════════════════════════════════════════
# ORCHESTRATOR DEMO COMMANDS
# ══════════════════════════════════════════════════════════════════════

COMMANDS = [
    "Assess current situation and identify top 3 critical issues",
    "3 volunteers cancelled their shifts today. Find replacements immediately.",
    (
        "Draft an emergency broadcast in Filipino and English about the "
        "evacuation center at Rizal High School now accepting displaced residents"
    ),
    "Generate a donor status report",
    (
        "Create a procurement task for emergency water supply — we currently "
        "have 500 liters but need 2000 liters for the next 3 days"
    ),
]


def _print_divider(char: str = "=", width: int = 50) -> None:
    print(char * width)


def _truncate(text: str, max_len: int = 120) -> str:
    if not text:
        return "—"
    text = str(text)
    return text[:max_len] + "..." if len(text) > max_len else text


async def run_demo_commands(disaster_id: int, db) -> None:
    for i, cmd in enumerate(COMMANDS, start=1):
        _print_divider()
        print(f"COMMAND {i}: {cmd}")
        _print_divider()

        try:
            result = await run_orchestrator(disaster_id, cmd, db)

            summary = result.get("orchestrator_summary", "—")
            agents  = result.get("agents_called", [])
            alerts  = result.get("alerts", [])
            actions = result.get("next_recommended_actions", [])
            ms      = result.get("execution_time_ms", 0)

            print(f"  Orchestrator Summary:")
            # Wrap long summaries
            words = summary.split()
            line, lines = [], []
            for w in words:
                line.append(w)
                if len(" ".join(line)) > 70:
                    lines.append("    " + " ".join(line))
                    line = []
            if line:
                lines.append("    " + " ".join(line))
            print("\n".join(lines))

            print(f"\n  Agents Called: {', '.join(agents) if agents else 'none'}")
            print(f"  Execution Time: {ms}ms")

            if alerts:
                print(f"\n  ⚠  Alerts ({len(alerts)}):")
                for a in alerts:
                    print(f"     • {_truncate(a, 100)}")

            if actions:
                print(f"\n  → Next Actions ({len(actions)}):")
                for a in actions:
                    print(f"     • {_truncate(a, 100)}")

        except Exception as exc:
            print(f"  ✗ Error: {exc}")

        _print_divider(char="-")
        print()

        # Small delay between commands to be kind to the API
        if i < len(COMMANDS):
            await asyncio.sleep(3)


# ══════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════

async def main() -> None:
    print()
    _print_divider()
    print("  ReliefAI — Demo Seeder")
    _print_divider()
    print()

    # ── Create tables ──────────────────────────────────────────────
    print("[1/3] Ensuring database tables exist...")
    await create_all_tables()
    print("  ✓ Tables ready.")
    print()

    # ── Seed data ──────────────────────────────────────────────────
    print("[2/3] Seeding Typhoon Carina scenario...")
    async with AsyncSessionLocal() as db:
        disaster_id = await seed(db)
    print()

    # ── Run demo commands ──────────────────────────────────────────
    print("[3/3] Running 5 orchestrator commands...")
    print()

    # Each command gets a fresh session so commits are isolated
    for i, cmd in enumerate(COMMANDS, start=1):
        _print_divider()
        print(f"COMMAND {i}: {cmd}")
        _print_divider()

        async with AsyncSessionLocal() as db:
            try:
                result = await run_orchestrator(disaster_id, cmd, db)

                summary = result.get("orchestrator_summary", "—")
                agents  = result.get("agents_called", [])
                alerts  = result.get("alerts", [])
                actions = result.get("next_recommended_actions", [])
                ms      = result.get("execution_time_ms", 0)

                print("  Orchestrator Summary:")
                words = str(summary).split()
                line: list[str] = []
                for w in words:
                    line.append(w)
                    if len(" ".join(line)) > 70:
                        print("    " + " ".join(line))
                        line = []
                if line:
                    print("    " + " ".join(line))

                print(f"\n  Agents Called : {', '.join(agents) if agents else 'none'}")
                print(f"  Execution Time: {ms}ms")

                if alerts:
                    print(f"\n  ⚠  Alerts ({len(alerts)}):")
                    for a in alerts:
                        print(f"     • {_truncate(a)}")

                if actions:
                    print(f"\n  → Next Actions ({len(actions)}):")
                    for a in actions:
                        print(f"     • {_truncate(a)}")

            except Exception as exc:
                print(f"  ✗ Error running command: {exc}")
                import traceback
                traceback.print_exc()

        _print_divider(char="-")
        print()

        if i < len(COMMANDS):
            await asyncio.sleep(3)

    # ── Final summary ──────────────────────────────────────────────
    _print_divider()
    print("  Demo seeding complete!")
    print(f"  Disaster ID: {disaster_id}")
    print()
    print("  Open http://localhost:8000 to view the dashboard")
    print("  Click 'Run Full Demo' button for instant presentation mode")
    _print_divider()
    print()


if __name__ == "__main__":
    asyncio.run(main())