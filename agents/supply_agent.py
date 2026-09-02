"""
Supply Chain Agent
Monitors supply levels, flags critical shortages, creates procurement tasks,
and suggests reallocation between categories.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List

from sqlalchemy.ext.asyncio import AsyncSession

import database.crud as crud
from database.models import SEVERITY_SCALE_MAX
from agents._groq_client import _call_groq

_AGENT_NAME = "supply_agent"

_CRITICAL_PCT = round(crud.CRITICAL_SUPPLY_THRESHOLD * 100)

_SYSTEM_PROMPT = (
    "You are Supply Chain Agent for disaster relief. "
    "Monitor supply levels against needs. "
    f"CRITICAL threshold: available < {_CRITICAL_PCT}% of needed. "
    "When critical items found, always create procurement tasks. "
    "Suggest reallocation when one category has surplus and another is critical. "
    "Prioritize: water > food > medical > shelter > equipment. "
    "You must respond with ONLY valid JSON: "
    "{critical_shortages: list[dict], tasks_created: list[dict], "
    "supplies_updated: list[dict], reallocation_suggestions: list[str], "
    "overall_supply_status: str}. "
    "No explanation, no markdown, no code blocks."
)

_PRIORITY_ORDER = ["water", "food", "medical", "shelter", "equipment"]


def _supply_line(s: Any, is_critical: bool) -> str:
    pct = (
        round(s.quantity_available / s.quantity_needed * 100, 1)
        if s.quantity_needed > 0
        else 0.0
    )
    flag = "⚠ CRITICAL" if is_critical else "ok"
    return (
        f"  - id={s.id} {s.item_name!r} category={s.category} "
        f"needed={s.quantity_needed}{s.unit} "
        f"available={s.quantity_available}{s.unit} ({pct}%) "
        f"priority={s.priority} status={s.status} [{flag}]"
    )


async def run_supply_agent(
    disaster_id: int, task: str, db: AsyncSession
) -> Dict[str, Any]:
    """
    Analyse supplies for *disaster_id*, create any DB tasks the model recommends,
    and return a structured result.
    """
    start_ts = time.time()

    try:
        # ── 1. Gather context ──────────────────────────────────────────────
        disaster = await crud.get_disaster(db, disaster_id)
        if disaster is None:
            return {"error": f"Disaster {disaster_id} not found.", "agent": _AGENT_NAME}

        all_supplies = await crud.list_supplies_by_disaster(db, disaster_id)
        critical_supplies = await crud.get_critical_supplies(db, disaster_id)
        critical_ids = {s.id for s in critical_supplies}

        existing_tasks = await crud.list_tasks(db, disaster_id, status_filter=None)
        procurement_tasks = [
            t for t in existing_tasks if "procure" in t.title.lower() or
            "supply" in t.title.lower() or "restock" in t.title.lower()
        ]

        # Group supplies by category for surplus detection
        category_totals: dict[str, dict] = {}
        for s in all_supplies:
            cat = s.category
            if cat not in category_totals:
                category_totals[cat] = {"needed": 0.0, "available": 0.0, "items": 0}
            category_totals[cat]["needed"] += s.quantity_needed
            category_totals[cat]["available"] += s.quantity_available
            category_totals[cat]["items"] += 1

        category_summary = "\n".join(
            f"  - {cat}: available={v['available']:.1f} / needed={v['needed']:.1f} "
            f"({round(v['available'] / v['needed'] * 100, 1) if v['needed'] > 0 else 0}%) "
            f"[{v['items']} items]"
            for cat, v in sorted(
                category_totals.items(),
                key=lambda kv: _PRIORITY_ORDER.index(kv[0])
                if kv[0] in _PRIORITY_ORDER
                else 99,
            )
        )

        supply_lines = "\n".join(
            _supply_line(s, s.id in critical_ids) for s in all_supplies
        )

        existing_task_lines = "\n".join(
            f"  - id={t.id} title={t.title!r} status={t.status} priority={t.priority}"
            for t in procurement_tasks
        ) or "  (none)"

        # ── 2. Build context ───────────────────────────────────────────────
        context = (
            f"DISASTER: id={disaster.id} name={disaster.name!r} "
            f"location={disaster.location!r} "
            f"severity={disaster.severity}/{SEVERITY_SCALE_MAX}\n\n"
            f"SUPPLY OVERVIEW BY CATEGORY:\n{category_summary}\n\n"
            f"ALL SUPPLY ITEMS ({len(all_supplies)}, "
            f"{len(critical_ids)} critical):\n{supply_lines}\n\n"
            f"EXISTING PROCUREMENT TASKS:\n{existing_task_lines}\n\n"
            f"TASK: {task}"
        )

        # ── 3. Call Groq ───────────────────────────────────────────────────
        result = await _call_groq(_SYSTEM_PROMPT, context)

    except Exception as exc:
        duration_ms = int((time.time() - start_ts) * 1000)
        await crud.create_log(
            db,
            agent_name=_AGENT_NAME,
            action="run_supply_agent",
            input_summary=f"disaster_id={disaster_id} task={task[:120]}",
            output_summary=f"ERROR: {exc}",
            status="error",
            duration_ms=duration_ms,
            disaster_id=disaster_id,
        )
        await db.commit()
        return {"error": str(exc), "agent": _AGENT_NAME}

    duration_ms = int((time.time() - start_ts) * 1000)

    # ── 4. Create tasks the model recommends ──────────────────────────────
    db_tasks_created: List[Dict[str, Any]] = []
    for task_spec in result.get("tasks_created", []):
        try:
            title = task_spec.get("title", "Procurement task")
            description = task_spec.get("description", "Auto-generated by supply_agent.")
            priority = task_spec.get("priority", "high")
            new_task = await crud.create_task(
                db,
                disaster_id=disaster_id,
                title=title,
                description=description,
                agent_assigned=_AGENT_NAME,
                priority=priority,
            )
            db_tasks_created.append(
                {"task_id": new_task.id, "title": new_task.title, "priority": new_task.priority}
            )
        except Exception:
            pass  # best-effort; don't abort whole agent run

    if db_tasks_created:
        result["tasks_created_in_db"] = db_tasks_created

    # ── 5. Log ─────────────────────────────────────────────────────────────
    await crud.create_log(
        db,
        agent_name=_AGENT_NAME,
        action="run_supply_agent",
        input_summary=f"disaster_id={disaster_id} task={task[:120]}",
        output_summary=(
            f"critical_shortages={len(result.get('critical_shortages', []))} "
            f"tasks_created={len(db_tasks_created)} "
            f"status={result.get('overall_supply_status', '')[:80]}"
        ),
        status="success",
        duration_ms=duration_ms,
        disaster_id=disaster_id,
    )
    await db.commit()

    result["agent"] = _AGENT_NAME
    result["execution_time_ms"] = duration_ms
    return result