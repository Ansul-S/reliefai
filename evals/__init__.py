"""
ReliefAI evaluation harness.

Unit tests prove the tool-calling loop *runs*. Evals measure whether the agent's
tool choices and answers are actually *good* — the thing that silently degrades
when a model is swapped, a prompt is edited, or a tool description is reworded.

Every case seeds a controlled scenario, runs the real orchestrator against it,
and scores the result on five dimensions (see ``evals.scorers``). Ground truth
is read back out of the database at run time rather than hardcoded, so the
scenarios stay the single source of truth.

Run with::

    python -m evals                 # everything
    python -m evals --list          # show cases
    python -m evals --case supply_shortage_identification
    python -m evals --tag grounding --json report.json

The evaluation database is redirected to a throwaway file before any project
module is imported, because ``database/__init__.py`` binds its engine at import
time. Set RELIEFAI_EVAL_DB to override the location.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_DEFAULT_EVAL_DB = Path(tempfile.gettempdir()) / "reliefai_evals.db"
_EVAL_DB = Path(os.environ.get("RELIEFAI_EVAL_DB", _DEFAULT_EVAL_DB))

# Must happen before `database` is imported anywhere in the process.
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_EVAL_DB}"

# The weather tool is the one tool that reaches outside the database. A live
# forecast would make scored runs non-deterministic — the same case would grade
# differently depending on the weather in Manila — so evals always use the
# fixed offline fixture. Set RELIEFAI_WEATHER_MODE explicitly to override.
os.environ.setdefault("RELIEFAI_WEATHER_MODE", "offline")

EVAL_DB_PATH = _EVAL_DB

__all__ = ["EVAL_DB_PATH"]
