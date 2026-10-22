"""Tests for guarantees the database layer makes to every writer."""
from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

import database.crud as crud


async def test_sqlite_enforces_foreign_keys(db):
    """
    SQLite ignores foreign keys unless each connection turns them on, so any
    writer could store a row that points at an operation that does not exist.
    """
    with pytest.raises(IntegrityError):
        await crud.create_task(db, disaster_id=424242, title="Orphan", description="-")
    await db.rollback()
