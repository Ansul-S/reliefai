"""
Tests for the eval runner's accounting.

A real case runs through the real runner, orchestrator and scorers; only the
SDK client is scripted (``fake_groq`` in conftest), so every model call goes
through the same metering path a live run uses.
"""
from __future__ import annotations

import json
import re

from evals.dataset import CASES_BY_NAME
from evals.runner import format_report, run_suite
from tests.conftest import groq_response


def _tool_turn(kwargs):
    """Ask for the supply table of whichever disaster the scenario created."""
    briefing = kwargs["messages"][1]["content"]
    did = int(re.search(r"Disaster id=(\d+)", briefing).group(1))
    return groq_response(
        tool_calls=[("call_1", "get_supply_status", json.dumps({"disaster_id": did}))],
        usage=(100, 10),
    )


async def test_metered_tokens_include_the_judge(fake_groq):
    """
    Regression: the suite's total summed only the orchestrator's own turns.
    The judge's call — and any tool that calls a model — never reached it, so
    reported spend sat well below what the provider's daily limit was charged.
    """
    fake_groq(
        _tool_turn,
        groq_response("I have the sandbag count.", usage=(120, 8)),
        groq_response(
            json.dumps({
                "orchestrator_summary": "450 sandbags available of 500 needed.",
                "alerts": [],
                "next_recommended_actions": [],
            }),
            usage=(130, 20),
        ),
        groq_response(
            json.dumps({
                "relevance": 5, "grounding": 5, "usefulness": 5,
                "reasoning": "Answers from the supply table.",
                "unsupported_claims": [],
            }),
            usage=(200, 30),
        ),
    )

    report = await run_suite([CASES_BY_NAME["simple_lookup_is_efficient"]])
    case = report["results"][0]

    assert case["passed"], case
    assert case["metered"] == {
        "agent_tokens": 388,
        "judge_tokens": 230,
        "total_tokens": 618,
        "calls": 4,
    }
    # The orchestrator-only figure keeps its meaning, for comparison with
    # earlier reports.
    assert report["total_tokens"] == 388
    assert report["metered_tokens"] == 618
    assert "618 tokens metered" in format_report(report)


async def test_a_case_that_fails_midway_still_reports_what_it_spent(fake_groq):
    """v4's errored case spent loop tokens before failing; none were recorded."""
    from tests.test_groq_client import _bad_request

    fake_groq(_tool_turn, groq_response("done", usage=(120, 8)), _bad_request())

    report = await run_suite(
        [CASES_BY_NAME["simple_lookup_is_efficient"]], use_judge=False
    )
    case = report["results"][0]

    assert "error" in case
    assert case["metered"]["agent_tokens"] == 238
    assert report["metered_tokens"] == 238
