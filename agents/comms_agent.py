"""
Communications Agent
Translates critical messages into six languages, enforces SMS character limits,
and persists draft broadcasts to the database.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List

from sqlalchemy.ext.asyncio import AsyncSession

import database.crud as crud
from agents._groq_client import _call_groq

_AGENT_NAME = "comms_agent"

_SYSTEM_PROMPT = (
    "You are Communications Agent for disaster relief. "
    "Translate critical messages into multiple languages. "
    "Target languages: English (en), Hindi (hi), Tamil (ta), "
    "Bahasa Indonesia (id), Filipino (tl), Japanese (ja). "
    "Rules: SMS must be under 160 characters. "
    "Use calm, clear language for public. "
    "Use urgent, technical language for coordinators. "
    "Never include unverified facts. "
    "Simplify — affected communities may be in shock. "
    "You must respond with ONLY valid JSON: "
    "{original_message: str, "
    "translations: {en: str, hi: str, ta: str, id: str, tl: str, ja: str}, "
    "sms_versions: {en: str, tl: str}, "
    "channel_recommendation: str, "
    "broadcast_id: int}. "
    "Set broadcast_id to 0 — it will be replaced by the database. "
    "No explanation, no markdown, no code blocks."
)

_DEFAULT_LANGUAGES = ["en", "hi", "ta", "id", "tl", "ja"]


async def run_comms_agent(
    disaster_id: int,
    message: str,
    languages: List[str],
    channel: str,
    db: AsyncSession,
) -> Dict[str, Any]:
    """
    Translate *message* into *languages*, save a draft Broadcast, and return
    the structured result including the real DB broadcast_id.
    """
    start_ts = time.time()

    try:
        # ── 1. Gather context ──────────────────────────────────────────────
        disaster = await crud.get_disaster(db, disaster_id)
        if disaster is None:
            return {"error": f"Disaster {disaster_id} not found.", "agent": _AGENT_NAME}

        # Determine audience type from channel / message heuristics
        audience = (
            "coordinators"
            if channel in ("email",)
            else "general_public"
        )

        target_langs = languages if languages else _DEFAULT_LANGUAGES
        valid_channels = {"sms", "email", "whatsapp", "all"}
        safe_channel = channel if channel in valid_channels else "all"

        # ── 2. Build user message ──────────────────────────────────────────
        user_message = (
            f"DISASTER: name={disaster.name!r} location={disaster.location!r} "
            f"severity={disaster.severity}/5\n\n"
            f"ORIGINAL MESSAGE:\n{message}\n\n"
            f"TARGET LANGUAGES: {', '.join(target_langs)}\n"
            f"DELIVERY CHANNEL: {safe_channel}\n"
            f"AUDIENCE TYPE: {audience}\n\n"
            "Produce all translations now. Keep SMS versions under 160 characters. "
            "For languages not in the target list, still produce them but they "
            "will be ignored — always include all six language keys."
        )

        # ── 3. Call Groq ───────────────────────────────────────────────────
        result = await _call_groq(_SYSTEM_PROMPT, user_message)

    except Exception as exc:
        duration_ms = int((time.time() - start_ts) * 1000)
        await crud.create_log(
            db,
            agent_name=_AGENT_NAME,
            action="run_comms_agent",
            input_summary=f"disaster_id={disaster_id} msg={message[:120]}",
            output_summary=f"ERROR: {exc}",
            status="error",
            duration_ms=duration_ms,
            disaster_id=disaster_id,
        )
        await db.commit()
        return {"error": str(exc), "agent": _AGENT_NAME}

    duration_ms = int((time.time() - start_ts) * 1000)

    # ── 4. Persist draft broadcast ─────────────────────────────────────────
    translations: Dict[str, str] = result.get("translations", {})
    sms_versions: Dict[str, str] = result.get("sms_versions", {})
    translations_payload = {**translations, "sms": sms_versions}

    try:
        broadcast = await crud.create_broadcast(
            db,
            disaster_id=disaster_id,
            message_en=translations.get("en", message),
            channel=safe_channel,
            target_audience=audience,
            message_translations=translations_payload,
            status="draft",
        )
        await db.commit()
        result["broadcast_id"] = broadcast.id
    except Exception as exc:
        result["broadcast_id"] = 0
        result["broadcast_save_error"] = str(exc)

    # ── 5. Log ─────────────────────────────────────────────────────────────
    await crud.create_log(
        db,
        agent_name=_AGENT_NAME,
        action="run_comms_agent",
        input_summary=f"disaster_id={disaster_id} channel={safe_channel} msg={message[:120]}",
        output_summary=(
            f"broadcast_id={result.get('broadcast_id', 0)} "
            f"languages={list(translations.keys())} "
            f"channel_rec={result.get('channel_recommendation', '')[:60]}"
        ),
        status="success",
        duration_ms=duration_ms,
        disaster_id=disaster_id,
    )
    await db.commit()

    result["agent"] = _AGENT_NAME
    result["execution_time_ms"] = duration_ms
    return result