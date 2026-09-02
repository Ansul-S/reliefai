from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


# ---------------------------------------------------------------------------
# Domain constants
#
# Values the agent legitimately quotes back to an operator ("severity 4/5").
# They are defined once here and surfaced in tool payloads rather than living
# only in prose, so that a number the agent states can always be traced to
# something a tool actually returned.
# ---------------------------------------------------------------------------

SEVERITY_SCALE_MIN = 1
SEVERITY_SCALE_MAX = 5


class Base(DeclarativeBase):
    pass


class Disaster(Base):
    __tablename__ = "disasters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[str] = mapped_column(String(100), nullable=False)
    location: Mapped[str] = mapped_column(String(255), nullable=False)
    severity: Mapped[int] = mapped_column(  # SEVERITY_SCALE_MIN..SEVERITY_SCALE_MAX
        Integer, nullable=False
    )
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    shifts: Mapped[List["Shift"]] = relationship("Shift", back_populates="disaster")
    supplies: Mapped[List["Supply"]] = relationship("Supply", back_populates="disaster")
    broadcasts: Mapped[List["Broadcast"]] = relationship(
        "Broadcast", back_populates="disaster"
    )
    tasks: Mapped[List["Task"]] = relationship("Task", back_populates="disaster")
    notes: Mapped[List["Note"]] = relationship("Note", back_populates="disaster")


class Volunteer(Base):
    __tablename__ = "volunteers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str] = mapped_column(String(50), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    skills: Mapped[List[Any]] = mapped_column(JSON, nullable=False, default=list)
    languages: Mapped[List[Any]] = mapped_column(JSON, nullable=False, default=list)
    availability: Mapped[Dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    shifts: Mapped[List["Shift"]] = relationship("Shift", back_populates="volunteer")


class Shift(Base):
    __tablename__ = "shifts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    disaster_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("disasters.id"), nullable=False
    )
    volunteer_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("volunteers.id"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(255), nullable=False)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="scheduled"
    )  # scheduled/confirmed/cancelled/completed
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    disaster: Mapped["Disaster"] = relationship("Disaster", back_populates="shifts")
    volunteer: Mapped["Volunteer"] = relationship("Volunteer", back_populates="shifts")


class Supply(Base):
    __tablename__ = "supplies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    disaster_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("disasters.id"), nullable=False
    )
    item_name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # food/medical/shelter/water/equipment
    quantity_needed: Mapped[float] = mapped_column(Float, nullable=False)
    quantity_available: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String(50), nullable=False)
    priority: Mapped[str] = mapped_column(
        String(20), nullable=False, default="medium"
    )  # high/medium/low
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="needed"
    )  # needed/procuring/available/depleted
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    disaster: Mapped["Disaster"] = relationship("Disaster", back_populates="supplies")


class Broadcast(Base):
    __tablename__ = "broadcasts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    disaster_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("disasters.id"), nullable=False
    )
    message_en: Mapped[str] = mapped_column(Text, nullable=False)
    message_translations: Mapped[Dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    channel: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # sms/email/whatsapp/all
    target_audience: Mapped[str] = mapped_column(String(255), nullable=False)
    sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="draft"
    )  # draft/sent/failed

    # Relationships
    disaster: Mapped["Disaster"] = relationship("Disaster", back_populates="broadcasts")


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    disaster_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("disasters.id"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    assigned_to: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    agent_assigned: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    priority: Mapped[str] = mapped_column(String(20), nullable=False, default="medium")
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="pending"
    )  # pending/in_progress/done/blocked
    due_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    disaster: Mapped["Disaster"] = relationship("Disaster", back_populates="tasks")


class AgentLog(Base):
    __tablename__ = "agent_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    disaster_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    agent_name: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(255), nullable=False)
    input_summary: Mapped[str] = mapped_column(Text, nullable=False)
    output_summary: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # success/error
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)

    # Tracing (nullable — populated by the tool-calling orchestrator)
    model: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    input_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Ordered list of {step: llm|tool, duration_ms, ...} records for one run
    trace: Mapped[Optional[List[Any]]] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Note(Base):
    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    disaster_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("disasters.id"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[List[Any]] = mapped_column(JSON, nullable=False, default=list)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    disaster: Mapped["Disaster"] = relationship("Disaster", back_populates="notes")