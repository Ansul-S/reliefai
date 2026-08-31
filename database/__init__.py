from __future__ import annotations

import os
from typing import Any, AsyncGenerator

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from database.models import Base

load_dotenv()

DATABASE_URL: str = os.getenv(
    "DATABASE_URL", "sqlite+aiosqlite:///./relief.db"
)

engine: AsyncEngine = create_async_engine(
    DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False},
)

AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an async database session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# Columns added to agent_logs after the initial release. SQLite cannot alter a
# table to add them via create_all, so they are applied additively on startup.
_AGENT_LOG_ADDED_COLUMNS: dict[str, str] = {
    "model": "VARCHAR(100)",
    "input_tokens": "INTEGER",
    "output_tokens": "INTEGER",
    "total_tokens": "INTEGER",
    "cost_usd": "FLOAT",
    "trace": "JSON",
}


async def create_all_tables() -> None:
    """Create all database tables if they do not already exist, then apply any
    additive column migrations to pre-existing tables."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _migrate_agent_logs(conn)


async def _migrate_agent_logs(conn: Any) -> None:
    """Add tracing columns to an agent_logs table created before they existed.

    Every column is nullable, so this is safe to run repeatedly and on a table
    that already holds rows.
    """
    if not DATABASE_URL.startswith("sqlite"):
        return

    result = await conn.exec_driver_sql("PRAGMA table_info(agent_logs)")
    existing = {row[1] for row in result.fetchall()}
    if not existing:
        return  # table does not exist yet; create_all handled it

    for column, sql_type in _AGENT_LOG_ADDED_COLUMNS.items():
        if column not in existing:
            await conn.exec_driver_sql(
                f"ALTER TABLE agent_logs ADD COLUMN {column} {sql_type}"
            )