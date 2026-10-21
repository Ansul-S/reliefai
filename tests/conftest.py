"""
Shared test fixtures.

The database URL is redirected to a throwaway SQLite file *before* any project
module is imported, because ``database/__init__.py`` binds its engine at import
time. Tests therefore never touch the developer's relief.db.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.gettempdir()) / "reliefai_test.db"
if _TMP_DB.exists():
    _TMP_DB.unlink()

os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP_DB}"
os.environ.setdefault("GROQ_API_KEY", "test-key-not-used")
os.environ.setdefault("API_KEY", "reliefai-demo-key")
# The limiter would otherwise start rejecting requests partway through a run
# and fail tests for a reason that has nothing to do with what they assert.
os.environ.setdefault("RELIEFAI_RATELIMIT", "off")

from types import SimpleNamespace  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from database import AsyncSessionLocal, create_all_tables  # noqa: E402
import database.crud as crud  # noqa: E402


@pytest_asyncio.fixture(scope="function")
async def db():
    """A session against a freshly created test database with one disaster."""
    await create_all_tables()
    async with AsyncSessionLocal() as session:
        yield session


@pytest_asyncio.fixture(scope="function")
async def seeded_disaster(db):
    """Create a disaster with one volunteer and one critical supply item."""
    disaster = await crud.create_disaster(
        db,
        name="Test Flood",
        type="flood",
        location="Testville",
        severity=3,
    )
    await db.flush()

    volunteer = await crud.create_volunteer(
        db,
        name="Test Volunteer",
        phone="+10000000000",
        email="volunteer@example.com",
        skills=["medical"],
        languages=["en"],
        availability={},
    )
    await db.flush()

    await crud.create_supply(
        db,
        disaster_id=disaster.id,
        item_name="Drinking Water",
        category="water",
        quantity_needed=1000.0,
        quantity_available=100.0,
        unit="liters",
        priority="high",
    )
    await db.commit()

    return {"disaster_id": disaster.id, "volunteer_id": volunteer.id}


class FakeLLM:
    """
    Scripted stand-in for ``_call_groq_with_tools``.

    Each queued turn is either a list of tool calls or a final text response,
    letting a test drive the loop deterministically without network access.
    """

    def __init__(self, turns):
        self.turns = list(turns)
        self.calls = []

    @staticmethod
    def tool_turn(*calls):
        """A turn where the model requests one or more tools."""
        return {
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call_{i}",
                        "type": "function",
                        "function": {"name": name, "arguments": args},
                    }
                    for i, (name, args) in enumerate(calls)
                ],
            },
            "finish_reason": "tool_calls",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
            },
            "latency_ms": 1,
        }

    @staticmethod
    def text_turn(content):
        """A turn where the model stops calling tools and answers."""
        return {
            "message": {"role": "assistant", "content": content, "tool_calls": None},
            "finish_reason": "stop",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
            },
            "latency_ms": 1,
        }

    async def __call__(self, messages, tools, **kwargs):
        self.calls.append({"messages": list(messages), "tools": tools, **kwargs})

        if not tools:
            # The finalising call passes no tools, so a real model cannot answer
            # with tool_calls. Skip any queued tool turns and give the next
            # textual answer.
            while self.turns and self.turns[0]["message"].get("tool_calls"):
                self.turns.pop(0)

        if not self.turns:
            # Exhausted script: behave like a model that is finished.
            return self.text_turn('{"orchestrator_summary": "done"}')
        return self.turns.pop(0)


@pytest.fixture
def fake_llm(monkeypatch):
    """Install a scripted FakeLLM in place of the real Groq client."""

    def _install(turns):
        llm = FakeLLM(turns)
        monkeypatch.setattr("agents.orchestrator._call_groq_with_tools", llm)
        return llm

    return _install


# ---------------------------------------------------------------------------
# A fake Groq SDK client
#
# FakeLLM replaces the orchestrator's call wholesale, which is right for the
# loop's control flow but skips everything inside agents/_groq_client.py.
# FakeGroq sits one level lower, in place of the SDK client, so retries and
# usage metering run for real.
# ---------------------------------------------------------------------------

GROQ_REQUEST = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")


def groq_response(content=None, tool_calls=(), usage=(10, 5)):
    """A chat completion shaped like the SDK's. ``tool_calls``: (id, name, args)."""
    calls = [
        SimpleNamespace(id=cid, function=SimpleNamespace(name=name, arguments=args))
        for cid, name, args in tool_calls
    ]
    message = SimpleNamespace(content=content, tool_calls=calls or None)
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=message,
                finish_reason="tool_calls" if calls else "stop",
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=usage[0],
            completion_tokens=usage[1],
            total_tokens=usage[0] + usage[1],
        ),
    )


@pytest.fixture
def fake_groq(monkeypatch):
    """
    Script the SDK client. Each step is a response, an exception to raise, or
    a callable taking the request's kwargs. Backoff is zeroed so retries are
    instant; steps carrying a "try again in Ns" hint would still really wait.
    """
    import agents._groq_client as groq_client

    monkeypatch.setattr(groq_client, "_BASE_DELAY", 0.0)

    def _install(*script):
        steps = list(script)
        state = {"clients": [], "requests": []}

        async def create(**kwargs):
            state["requests"].append(kwargs)
            step = steps.pop(0)
            if isinstance(step, BaseException):
                raise step
            return step(kwargs) if callable(step) else step

        def client(**kwargs):
            state["clients"].append(kwargs)
            return SimpleNamespace(
                chat=SimpleNamespace(completions=SimpleNamespace(create=create))
            )

        monkeypatch.setattr(groq_client, "AsyncGroq", client)
        return state

    return _install
