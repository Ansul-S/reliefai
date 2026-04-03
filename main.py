"""
ReliefAI — FastAPI backend entry point.
Phase 3: Complete REST API wiring all DB, agent, and MCP layers.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession

from database import create_all_tables, get_db
import database.crud as crud
from agents.orchestrator import run_orchestrator
from agents.supply_agent import run_supply_agent
from agents.comms_agent import run_comms_agent
from agents.shift_agent import run_shift_agent
from agents.report_agent import run_report_agent
from schemas.models import (
    AgentLogResponse,
    AssignVolunteer,
    BroadcastRequest,
    DisasterCreate,
    OrchestratorCommand,
    ShiftCreate,
    StandardResponse,
    SupplyCreate,
    SupplyUpdate,
    TaskCreate,
    TaskUpdate,
    VolunteerCreate,
)

# ──────────────────────────────────────────────────────────────────────────────
# App lifecycle
# ──────────────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_all_tables()
    # Auto-seed on first run (non-blocking — we just fire it)
    from database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        try:
            await _seed_if_empty(db)
        except Exception:
            pass  # Never block startup
    yield


app = FastAPI(
    title="ReliefAI — Post-Disaster Relief Orchestrator",
    description="AI-powered disaster relief coordination backend.",
    version="1.0.0",
    lifespan=lifespan,
)

# ──────────────────────────────────────────────────────────────────────────────
# Middleware
# ──────────────────────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ──────────────────────────────────────────────────────────────────────────────
# Static files
# ──────────────────────────────────────────────────────────────────────────────

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(_STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=_STATIC_DIR, html=True), name="static")


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/static/index.html")


# ──────────────────────────────────────────────────────────────────────────────
# Auth dependency
# ──────────────────────────────────────────────────────────────────────────────

_API_KEY = os.getenv("API_KEY", "reliefai-demo-key")


async def verify_api_key(x_api_key: str = Header(..., alias="X-API-Key")):
    if x_api_key != _API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")
    return x_api_key


# ──────────────────────────────────────────────────────────────────────────────
# Global error handlers
# ──────────────────────────────────────────────────────────────────────────────


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"success": False, "data": None, "error": str(exc)},
    )


@app.exception_handler(422)
async def validation_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=422,
        content={
            "success": False,
            "data": None,
            "error": f"Validation error: {exc}",
        },
    )


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _ok(data: Any = None) -> StandardResponse:
    return StandardResponse(success=True, data=data)


def _err(msg: str) -> StandardResponse:
    return StandardResponse(success=False, error=msg)


def _supply_dict(s: Any, is_critical: bool = False) -> Dict[str, Any]:
    return {
        "id": s.id,
        "disaster_id": s.disaster_id,
        "item_name": s.item_name,
        "category": s.category,
        "quantity_needed": s.quantity_needed,
        "quantity_available": s.quantity_available,
        "unit": s.unit,
        "priority": s.priority,
        "status": s.status,
        "is_critical": is_critical,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


def _disaster_dict(d: Any, **extras) -> Dict[str, Any]:
    return {
        "id": d.id,
        "name": d.name,
        "type": d.type,
        "location": d.location,
        "severity": d.severity,
        "status": d.status,
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "updated_at": d.updated_at.isoformat() if d.updated_at else None,
        **extras,
    }


def _volunteer_dict(v: Any, **extras) -> Dict[str, Any]:
    return {
        "id": v.id,
        "name": v.name,
        "phone": v.phone,
        "email": v.email,
        "skills": v.skills or [],
        "languages": v.languages or [],
        "availability": v.availability or {},
        "is_active": v.is_active,
        "created_at": v.created_at.isoformat() if v.created_at else None,
        **extras,
    }


def _shift_dict(s: Any) -> Dict[str, Any]:
    vol_name = s.volunteer.name if hasattr(s, "volunteer") and s.volunteer else None
    return {
        "id": s.id,
        "disaster_id": s.disaster_id,
        "volunteer_id": s.volunteer_id,
        "volunteer_name": vol_name,
        "role": s.role,
        "start_time": s.start_time.isoformat() if s.start_time else None,
        "end_time": s.end_time.isoformat() if s.end_time else None,
        "status": s.status,
        "notes": s.notes,
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }


def _task_dict(t: Any) -> Dict[str, Any]:
    return {
        "id": t.id,
        "disaster_id": t.disaster_id,
        "title": t.title,
        "description": t.description,
        "assigned_to": t.assigned_to,
        "agent_assigned": t.agent_assigned,
        "priority": t.priority,
        "status": t.status,
        "due_date": t.due_date.isoformat() if t.due_date else None,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "completed_at": t.completed_at.isoformat() if t.completed_at else None,
    }


def _broadcast_dict(b: Any) -> Dict[str, Any]:
    return {
        "id": b.id,
        "disaster_id": b.disaster_id,
        "message_en": b.message_en,
        "message_translations": b.message_translations or {},
        "channel": b.channel,
        "target_audience": b.target_audience,
        "sent_at": b.sent_at.isoformat() if b.sent_at else None,
        "status": b.status,
    }


def _log_dict(lg: Any) -> Dict[str, Any]:
    return {
        "id": lg.id,
        "disaster_id": lg.disaster_id,
        "agent_name": lg.agent_name,
        "action": lg.action,
        "input_summary": lg.input_summary,
        "output_summary": lg.output_summary,
        "status": lg.status,
        "duration_ms": lg.duration_ms,
        "created_at": lg.created_at.isoformat() if lg.created_at else None,
    }


# ──────────────────────────────────────────────────────────────────────────────
# HEALTH
# ──────────────────────────────────────────────────────────────────────────────


@app.get("/health", tags=["Health"])
async def health():
    return {
        "status": "ok",
        "db": "connected",
        "ai_provider": "groq",
        "model": "llama-3.3-70b-versatile",
        "version": "1.0.0",
    }


# ──────────────────────────────────────────────────────────────────────────────
# DISASTERS
# ──────────────────────────────────────────────────────────────────────────────


@app.post("/disasters", tags=["Disasters"])
async def create_disaster(
    body: DisasterCreate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    disaster = await crud.create_disaster(
        db,
        name=body.name,
        type=body.type,
        location=body.location,
        severity=body.severity,
    )
    return _ok(_disaster_dict(disaster))


@app.get("/disasters", tags=["Disasters"])
async def list_disasters(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    disasters = await crud.list_disasters(db)
    return _ok([_disaster_dict(d) for d in disasters])


@app.get("/disasters/{disaster_id}", tags=["Disasters"])
async def get_disaster(
    disaster_id: int,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    disaster = await crud.get_disaster(db, disaster_id)
    if not disaster:
        return _err(f"Disaster {disaster_id} not found.")

    supplies = await crud.list_supplies_by_disaster(db, disaster_id)
    critical = await crud.get_critical_supplies(db, disaster_id)
    tasks = await crud.list_tasks(db, disaster_id)
    today_shifts = await crud.list_shifts_by_disaster(
        db, disaster_id, datetime.now(tz=timezone.utc).date()
    )
    volunteers = await crud.list_volunteers_by_disaster(db, disaster_id)

    return _ok(
        _disaster_dict(
            disaster,
            supplies_total=len(supplies),
            critical_supplies=len(critical),
            tasks_open=len([t for t in tasks if t.status != "done"]),
            shifts_today=len(today_shifts),
            volunteers_assigned=len(volunteers),
        )
    )


# ──────────────────────────────────────────────────────────────────────────────
# ORCHESTRATOR  ★  primary demo endpoint
# ──────────────────────────────────────────────────────────────────────────────


@app.post("/disasters/{disaster_id}/command", tags=["Orchestrator"])
async def run_command(
    disaster_id: int,
    body: OrchestratorCommand,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    disaster = await crud.get_disaster(db, disaster_id)
    if not disaster:
        return _err(f"Disaster {disaster_id} not found.")

    result = await run_orchestrator(disaster_id, body.command, db)

    if "error" in result and len(result) <= 2:
        return StandardResponse(success=False, data=None, error=result["error"])

    return _ok(result)


# ──────────────────────────────────────────────────────────────────────────────
# VOLUNTEERS
# ──────────────────────────────────────────────────────────────────────────────


@app.post("/volunteers", tags=["Volunteers"])
async def create_volunteer(
    body: VolunteerCreate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    vol = await crud.create_volunteer(
        db,
        name=body.name,
        phone=body.phone,
        email=body.email,
        skills=body.skills,
        languages=body.languages,
        availability=body.availability,
    )
    return _ok(_volunteer_dict(vol))


@app.get("/disasters/{disaster_id}/volunteers", tags=["Volunteers"])
async def list_disaster_volunteers(
    disaster_id: int,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    disaster = await crud.get_disaster(db, disaster_id)
    if not disaster:
        return _err(f"Disaster {disaster_id} not found.")

    volunteers = await crud.list_volunteers_by_disaster(db, disaster_id)
    result = []
    for v in volunteers:
        hours_today = await crud.get_volunteer_hours_today(db, v.id)
        result.append(_volunteer_dict(v, hours_today=hours_today))
    return _ok(result)


@app.post(
    "/disasters/{disaster_id}/volunteers/{volunteer_id}/assign",
    tags=["Volunteers"],
)
async def assign_volunteer(
    disaster_id: int,
    volunteer_id: int,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    disaster = await crud.get_disaster(db, disaster_id)
    if not disaster:
        return _err(f"Disaster {disaster_id} not found.")

    volunteer = await crud.get_volunteer(db, volunteer_id)
    if not volunteer:
        return _err(f"Volunteer {volunteer_id} not found.")

    # Create a placeholder "on-call" shift for the next 24 hours
    now = datetime.now(tz=timezone.utc)
    shift = await crud.create_shift(
        db,
        disaster_id=disaster_id,
        volunteer_id=volunteer_id,
        role="on-call",
        start_time=now,
        end_time=now + timedelta(hours=24),
        notes="Assigned via API — placeholder shift.",
    )
    return _ok(
        {
            "message": f"Volunteer '{volunteer.name}' assigned to disaster '{disaster.name}'.",
            "shift_id": shift.id,
            "volunteer": _volunteer_dict(volunteer),
        }
    )


# ──────────────────────────────────────────────────────────────────────────────
# SUPPLIES
# ──────────────────────────────────────────────────────────────────────────────


@app.get("/disasters/{disaster_id}/supplies", tags=["Supplies"])
async def list_supplies(
    disaster_id: int,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    disaster = await crud.get_disaster(db, disaster_id)
    if not disaster:
        return _err(f"Disaster {disaster_id} not found.")

    supplies = await crud.list_supplies_by_disaster(db, disaster_id)
    critical_ids = {s.id for s in await crud.get_critical_supplies(db, disaster_id)}
    return _ok([_supply_dict(s, s.id in critical_ids) for s in supplies])


@app.post("/disasters/{disaster_id}/supplies", tags=["Supplies"])
async def create_supply(
    disaster_id: int,
    body: SupplyCreate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    disaster = await crud.get_disaster(db, disaster_id)
    if not disaster:
        return _err(f"Disaster {disaster_id} not found.")

    supply = await crud.create_supply(
        db,
        disaster_id=disaster_id,
        item_name=body.item_name,
        category=body.category,
        quantity_needed=body.quantity_needed,
        quantity_available=body.quantity_available,
        unit=body.unit,
        priority=body.priority,
    )
    is_critical = (
        supply.quantity_available / supply.quantity_needed < 0.3
        if supply.quantity_needed > 0
        else False
    )
    return _ok(_supply_dict(supply, is_critical))


@app.patch("/supplies/{supply_id}", tags=["Supplies"])
async def update_supply(
    supply_id: int,
    body: SupplyUpdate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    supply = await crud.update_supply(
        db,
        supply_id,
        quantity_available=body.quantity_available,
        status=body.status,
    )
    if not supply:
        return _err(f"Supply {supply_id} not found.")

    is_critical = (
        supply.quantity_available / supply.quantity_needed < 0.3
        if supply.quantity_needed > 0
        else False
    )
    return _ok(_supply_dict(supply, is_critical))


@app.post("/disasters/{disaster_id}/supplies/assess", tags=["Supplies"])
async def assess_supplies(
    disaster_id: int,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    disaster = await crud.get_disaster(db, disaster_id)
    if not disaster:
        return _err(f"Disaster {disaster_id} not found.")

    result = await run_supply_agent(
        disaster_id,
        "Full supply assessment and procurement recommendations",
        db,
    )
    return _ok(result)


# ──────────────────────────────────────────────────────────────────────────────
# SHIFTS
# ──────────────────────────────────────────────────────────────────────────────


@app.get("/disasters/{disaster_id}/shifts", tags=["Shifts"])
async def list_shifts(
    disaster_id: int,
    date: Optional[str] = Query(None, description="Filter by date YYYY-MM-DD"),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    disaster = await crud.get_disaster(db, disaster_id)
    if not disaster:
        return _err(f"Disaster {disaster_id} not found.")

    date_obj = None
    if date:
        try:
            from datetime import date as date_type
            date_obj = date_type.fromisoformat(date)
        except ValueError:
            return _err(f"Invalid date format '{date}'. Use YYYY-MM-DD.")

    shifts = await crud.list_shifts_by_disaster(db, disaster_id, date_obj)
    return _ok([_shift_dict(s) for s in shifts])


@app.post("/disasters/{disaster_id}/shifts", tags=["Shifts"])
async def create_shift(
    disaster_id: int,
    body: ShiftCreate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    disaster = await crud.get_disaster(db, disaster_id)
    if not disaster:
        return _err(f"Disaster {disaster_id} not found.")

    volunteer = await crud.get_volunteer(db, body.volunteer_id)
    if not volunteer:
        return _err(f"Volunteer {body.volunteer_id} not found.")

    if body.end_time <= body.start_time:
        return _err("end_time must be after start_time.")

    shift = await crud.create_shift(
        db,
        disaster_id=disaster_id,
        volunteer_id=body.volunteer_id,
        role=body.role,
        start_time=body.start_time,
        end_time=body.end_time,
        notes=body.notes,
    )
    shift = await crud.get_shift(db, shift.id)
    return _ok(_shift_dict(shift))


@app.post("/disasters/{disaster_id}/shifts/gaps", tags=["Shifts"])
async def detect_and_fill_gaps(
    disaster_id: int,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    disaster = await crud.get_disaster(db, disaster_id)
    if not disaster:
        return _err(f"Disaster {disaster_id} not found.")

    result = await run_shift_agent(disaster_id, db)
    return _ok(result)


# ──────────────────────────────────────────────────────────────────────────────
# BROADCASTS
# ──────────────────────────────────────────────────────────────────────────────


@app.get("/disasters/{disaster_id}/broadcasts", tags=["Broadcasts"])
async def list_broadcasts(
    disaster_id: int,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    disaster = await crud.get_disaster(db, disaster_id)
    if not disaster:
        return _err(f"Disaster {disaster_id} not found.")

    broadcasts = await crud.list_broadcasts(db, disaster_id)
    return _ok([_broadcast_dict(b) for b in broadcasts])


@app.post("/disasters/{disaster_id}/broadcasts", tags=["Broadcasts"])
async def create_broadcast(
    disaster_id: int,
    body: BroadcastRequest,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    disaster = await crud.get_disaster(db, disaster_id)
    if not disaster:
        return _err(f"Disaster {disaster_id} not found.")

    result = await run_comms_agent(
        disaster_id=disaster_id,
        message=body.message,
        languages=body.languages,
        channel=body.channel,
        db=db,
    )
    return _ok(result)


# ──────────────────────────────────────────────────────────────────────────────
# REPORTS
# ──────────────────────────────────────────────────────────────────────────────


@app.get("/disasters/{disaster_id}/report", tags=["Reports"])
async def get_report(
    disaster_id: int,
    type: str = Query("internal", description="donor | authority | internal"),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    disaster = await crud.get_disaster(db, disaster_id)
    if not disaster:
        return _err(f"Disaster {disaster_id} not found.")

    valid = {"donor", "authority", "internal"}
    report_type = type if type in valid else "internal"
    result = await run_report_agent(disaster_id, report_type, db)
    return _ok(result)


# ──────────────────────────────────────────────────────────────────────────────
# TASKS
# ──────────────────────────────────────────────────────────────────────────────


@app.get("/disasters/{disaster_id}/tasks", tags=["Tasks"])
async def list_tasks(
    disaster_id: int,
    status: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    disaster = await crud.get_disaster(db, disaster_id)
    if not disaster:
        return _err(f"Disaster {disaster_id} not found.")

    tasks = await crud.list_tasks(db, disaster_id, status_filter=status)
    return _ok([_task_dict(t) for t in tasks])


@app.post("/disasters/{disaster_id}/tasks", tags=["Tasks"])
async def create_task(
    disaster_id: int,
    body: TaskCreate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    disaster = await crud.get_disaster(db, disaster_id)
    if not disaster:
        return _err(f"Disaster {disaster_id} not found.")

    task = await crud.create_task(
        db,
        disaster_id=disaster_id,
        title=body.title,
        description=body.description,
        assigned_to=body.assigned_to,
        priority=body.priority,
        due_date=body.due_date,
    )
    return _ok(_task_dict(task))


@app.patch("/tasks/{task_id}", tags=["Tasks"])
async def update_task(
    task_id: int,
    body: TaskUpdate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    task = await crud.update_task_status(db, task_id, body.status, body.notes)
    if not task:
        return _err(f"Task {task_id} not found.")
    return _ok(_task_dict(task))


# ──────────────────────────────────────────────────────────────────────────────
# AGENT LOGS
# ──────────────────────────────────────────────────────────────────────────────


@app.get("/disasters/{disaster_id}/logs", tags=["Logs"])
async def list_agent_logs(
    disaster_id: int,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    disaster = await crud.get_disaster(db, disaster_id)
    if not disaster:
        return _err(f"Disaster {disaster_id} not found.")

    logs = await crud.list_logs(db, disaster_id, limit=50)
    return _ok([_log_dict(lg) for lg in logs])


# ──────────────────────────────────────────────────────────────────────────────
# DEMO ENDPOINTS
# ──────────────────────────────────────────────────────────────────────────────

_DEMO_DISASTER_NAME = "Typhoon Carina — Metro Manila, Philippines"


async def _seed_if_empty(db: AsyncSession) -> Optional[int]:
    """
    Idempotent seed: creates full demo dataset if no disaster named
    _DEMO_DISASTER_NAME exists. Returns the disaster_id.
    """
    disasters = await crud.list_disasters(db)
    for d in disasters:
        if d.name == _DEMO_DISASTER_NAME:
            return d.id  # already seeded

    # ── Disaster ──────────────────────────────────────────────────────────
    disaster = await crud.create_disaster(
        db,
        name=_DEMO_DISASTER_NAME,
        type="typhoon",
        location="Metro Manila, Philippines",
        severity=4,
    )
    did = disaster.id

    # ── Volunteers ────────────────────────────────────────────────────────
    volunteers_data = [
        ("Maria Santos",    "+63-917-000-0001", "maria@relief.ph",    ["medical", "first_aid"],         ["Filipino", "English"]),
        ("Juan dela Cruz",  "+63-917-000-0002", "juan@relief.ph",     ["logistics", "driving"],          ["Filipino", "English"]),
        ("Ana Reyes",       "+63-917-000-0003", "ana@relief.ph",      ["construction", "shelter"],       ["Filipino", "Bahasa Indonesia"]),
        ("Carlos Bautista", "+63-917-000-0004", "carlos@relief.ph",   ["medical", "counseling"],         ["Filipino", "English", "Spanish"]),
        ("Priya Sharma",    "+91-985-000-0001", "priya@relief.in",    ["medical", "logistics"],          ["Hindi", "English", "Tamil"]),
        ("Ravi Kumar",      "+91-985-000-0002", "ravi@relief.in",     ["driving", "construction"],       ["Hindi", "English"]),
        ("Siti Rahayu",     "+62-812-000-0001", "siti@relief.id",     ["logistics", "communication"],    ["Bahasa Indonesia", "English"]),
        ("Ahmad Fauzi",     "+62-812-000-0002", "ahmad@relief.id",    ["medical", "driving"],            ["Bahasa Indonesia", "English"]),
        ("James Tan",       "+65-900-000-0001", "james@relief.sg",    ["shelter", "logistics"],          ["English", "Filipino"]),
        ("Grace Lim",       "+65-900-000-0002", "grace@relief.sg",    ["medical", "first_aid", "counseling"], ["English", "Filipino", "Tamil"]),
        ("Pedro Villanueva","+63-917-000-0005", "pedro@relief.ph",    ["driving", "logistics"],          ["Filipino", "English"]),
        ("Rosa Fernandez",  "+63-917-000-0006", "rosa@relief.ph",     ["communication", "counseling"],   ["Filipino", "English", "Hindi"]),
    ]
    volunteer_ids: List[int] = []
    for name, phone, email, skills, langs in volunteers_data:
        v = await crud.create_volunteer(db, name, phone, email, skills, langs)
        volunteer_ids.append(v.id)

    # ── Supplies ──────────────────────────────────────────────────────────
    supplies_data = [
        ("Rice",             "food",      500.0,  120.0,  "kg",    "high"),
        ("Drinking Water",   "water",    2000.0,  500.0,  "liters","high"),
        ("Baby Formula",     "food",      100.0,   10.0,  "cans",  "high"),
        ("Medical Kits",     "medical",    50.0,   48.0,  "units", "medium"),
        ("Emergency Tents",  "shelter",   100.0,   30.0,  "units", "high"),
        ("Generators",       "equipment",  10.0,    3.0,  "units", "medium"),
        ("Blankets",         "shelter",   200.0,  180.0,  "units", "low"),
        ("Sanitation Kits",  "medical",   150.0,   90.0,  "units", "medium"),
    ]
    for item_name, category, needed, available, unit, priority in supplies_data:
        await crud.create_supply(
            db, did, item_name, category, needed, available, unit, priority
        )

    # ── Shifts (today, mix of statuses) ───────────────────────────────────
    now = datetime.now(tz=timezone.utc)
    today_start = now.replace(hour=6, minute=0, second=0, microsecond=0)
    shifts_data = [
        (volunteer_ids[0], "Medical Triage",       today_start,               today_start + timedelta(hours=8),  "scheduled"),
        (volunteer_ids[1], "Logistics Coordinator", today_start,               today_start + timedelta(hours=8),  "confirmed"),
        (volunteer_ids[2], "General Distribution", today_start + timedelta(hours=8), today_start + timedelta(hours=16), "scheduled"),
        (volunteer_ids[3], "Medical Support",      today_start + timedelta(hours=8), today_start + timedelta(hours=16), "cancelled"),
        (volunteer_ids[4], "Field Medical",        today_start + timedelta(hours=16), today_start + timedelta(hours=24), "cancelled"),
        (volunteer_ids[5], "Night Logistics",      today_start + timedelta(hours=16), today_start + timedelta(hours=24), "cancelled"),
    ]
    for vol_id, role, start, end, status in shifts_data:
        shift = await crud.create_shift(db, did, vol_id, role, start, end)
        if status != "scheduled":
            if status == "confirmed":
                shift.status = "confirmed"
            elif status == "cancelled":
                shift.status = "cancelled"

    # ── Notes ─────────────────────────────────────────────────────────────
    notes_data = [
        (
            "Field Report 14:00: Barangay 143 flooded to 1.5m. Approximately 340 "
            "families displaced. Access via Quirino Ave still passable.",
            ["field-report", "flooding", "access"],
            "field_team_alpha",
        ),
        (
            "Weather Update: PAGASA forecasts additional 80-120mm rainfall in next "
            "12 hours. Wind speed 75kph sustained. Typhoon signal 3 remains in effect.",
            ["weather", "pagasa", "urgent"],
            "weather_monitor",
        ),
        (
            "Supply Observation: Baby formula critically low — only 10 cans remain "
            "vs 100 needed. Multiple infants at evacuation center. URGENT procurement "
            "required. Contact Generika and Mercury Drug branches still operational.",
            ["supply", "critical", "baby-formula", "procurement"],
            "supply_coordinator",
        ),
    ]
    for content, tags, created_by in notes_data:
        await crud.create_note(db, did, content, created_by, tags)

    await db.commit()
    return did


@app.post("/demo/seed", tags=["Demo"])
async def demo_seed(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    """
    Idempotent demo seed. Safe to call multiple times — skips if already seeded.
    """
    disasters = await crud.list_disasters(db)
    for d in disasters:
        if d.name == _DEMO_DISASTER_NAME:
            return _ok({"disaster_id": d.id, "message": "Demo already seeded — skipping."})

    disaster_id = await _seed_if_empty(db)
    return _ok({"disaster_id": disaster_id, "message": "Demo seeded successfully."})


@app.post("/demo/simulate", tags=["Demo"])
async def demo_simulate(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    """
    Run 5 representative orchestrator commands in sequence and return all results.
    Seeds demo data first if needed.
    """
    # Ensure seeded
    disasters = await crud.list_disasters(db)
    disaster_id: Optional[int] = None
    for d in disasters:
        if d.name == _DEMO_DISASTER_NAME:
            disaster_id = d.id
            break

    if disaster_id is None:
        disaster_id = await _seed_if_empty(db)

    commands = [
        "Assess current situation and identify top 3 critical issues",
        "3 volunteers cancelled Saturday night shifts. Find replacements immediately.",
        (
            "Draft emergency broadcast in Filipino and English about evacuation "
            "center opening at Rizal High School"
        ),
        "Generate a donor status report",
        (
            "Create procurement task for emergency water — "
            "we have 500L but need 2000L for 3 days"
        ),
    ]

    results: List[Dict[str, Any]] = []
    for cmd in commands:
        try:
            res = await run_orchestrator(disaster_id, cmd, db)
            results.append({"command": cmd, "result": res})
        except Exception as exc:
            results.append({"command": cmd, "error": str(exc)})

    return _ok({"commands_run": len(commands), "disaster_id": disaster_id, "results": results})


# ──────────────────────────────────────────────────────────────────────────────
# Dev entry point
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",
    )