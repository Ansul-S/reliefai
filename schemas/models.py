"""
Pydantic v2 schemas for ReliefAI API request and response shapes.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


# ═══════════════════════════════════════════════════════════════════
# Shared config
# ═══════════════════════════════════════════════════════════════════

class _Base(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ═══════════════════════════════════════════════════════════════════
# REQUEST MODELS
# ═══════════════════════════════════════════════════════════════════

class DisasterCreate(_Base):
    name: str = Field(..., min_length=2, max_length=255)
    type: str = Field(..., min_length=2, max_length=100)
    location: str = Field(..., min_length=2, max_length=255)
    severity: int = Field(..., ge=1, le=5)


class VolunteerCreate(_Base):
    name: str = Field(..., min_length=2, max_length=255)
    phone: str = Field(..., min_length=5, max_length=50)
    email: str = Field(..., min_length=5, max_length=255)
    skills: List[str] = Field(default_factory=list)
    languages: List[str] = Field(default_factory=list)
    availability: Dict[str, Any] = Field(default_factory=dict)


class ShiftCreate(_Base):
    volunteer_id: int
    role: str = Field(..., min_length=2, max_length=255)
    start_time: datetime
    end_time: datetime
    notes: Optional[str] = None


class SupplyCreate(_Base):
    item_name: str = Field(..., min_length=2, max_length=255)
    category: str = Field(..., pattern="^(food|medical|shelter|water|equipment)$")
    quantity_needed: float = Field(..., gt=0)
    quantity_available: float = Field(..., ge=0)
    unit: str = Field(..., min_length=1, max_length=50)
    priority: str = Field(default="medium", pattern="^(high|medium|low)$")


class SupplyUpdate(_Base):
    quantity_available: float = Field(..., ge=0)
    status: str = Field(..., pattern="^(needed|procuring|available|depleted)$")


class BroadcastRequest(_Base):
    message: str = Field(..., min_length=5)
    languages: List[str] = Field(default_factory=lambda: ["en", "hi", "ta", "id", "tl", "ja"])
    channel: str = Field(default="all", pattern="^(sms|email|whatsapp|all)$")


class TaskCreate(_Base):
    title: str = Field(..., min_length=2, max_length=255)
    description: str = Field(..., min_length=5)
    assigned_to: Optional[str] = None
    priority: str = Field(default="medium", pattern="^(high|medium|low)$")
    due_date: Optional[datetime] = None


class TaskUpdate(_Base):
    status: str = Field(..., pattern="^(pending|in_progress|done|blocked)$")
    notes: Optional[str] = None


class OrchestratorCommand(_Base):
    command: str = Field(..., min_length=5)


class AssignVolunteer(_Base):
    volunteer_id: int


# ═══════════════════════════════════════════════════════════════════
# RESPONSE MODELS
# ═══════════════════════════════════════════════════════════════════

class DisasterResponse(_Base):
    id: int
    name: str
    type: str
    location: str
    severity: int
    status: str
    created_at: Optional[datetime] = None


class VolunteerResponse(_Base):
    id: int
    name: str
    phone: str
    email: str
    skills: List[str] = Field(default_factory=list)
    languages: List[str] = Field(default_factory=list)
    is_active: bool


class ShiftResponse(_Base):
    id: int
    disaster_id: int
    volunteer_id: int
    role: str
    start_time: datetime
    end_time: datetime
    status: str
    notes: Optional[str] = None


class SupplyResponse(_Base):
    id: int
    item_name: str
    category: str
    quantity_needed: float
    quantity_available: float
    unit: str
    priority: str
    status: str
    is_critical: bool = False


class BroadcastResponse(_Base):
    id: int
    message_en: str
    message_translations: Dict[str, Any] = Field(default_factory=dict)
    channel: str
    status: str
    sent_at: Optional[datetime] = None


class TaskResponse(_Base):
    id: int
    title: str
    description: str
    assigned_to: Optional[str] = None
    priority: str
    status: str
    due_date: Optional[datetime] = None
    created_at: Optional[datetime] = None


class OrchestratorResponse(_Base):
    orchestrator_summary: str = ""
    agents_called: List[str] = Field(default_factory=list)
    results: Dict[str, Any] = Field(default_factory=dict)
    alerts: List[str] = Field(default_factory=list)
    next_recommended_actions: List[str] = Field(default_factory=list)
    execution_time_ms: int = 0


class AgentLogResponse(_Base):
    id: int
    agent_name: str
    action: str
    status: str
    duration_ms: int
    created_at: Optional[datetime] = None


class StandardResponse(_Base):
    success: bool = True
    data: Optional[Any] = None
    error: Optional[str] = None