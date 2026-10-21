"""
Evaluation runner.

Each case: seed its scenario into a clean database, snapshot row counts, run the
real orchestrator, snapshot again, then score. Cases run sequentially by default
because they share one database and Groq's free tier rate-limits hard.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import func, select

from agents._groq_client import track_usage
from database import AsyncSessionLocal, create_all_tables
from database.models import Broadcast, Note, Shift, Supply, Task, Volunteer
from evals.dataset import CASES, CASES_BY_NAME, EvalCase
from evals.scenarios import SCENARIOS
from evals.scorers import (
    score_grounding,
    score_judge,
    score_loop_health,
    score_side_effects,
    score_tool_selection,
)

_COUNTED_TABLES = {
    "tasks": Task,
    "shifts": Shift,
    "supplies": Supply,
    "broadcasts": Broadcast,
    "notes": Note,
    "volunteers": Volunteer,
}

# A case passes when its weighted score clears this and no dimension hard-fails
# in a way the weights would mask.
PASS_THRESHOLD = 0.75

# Substrings identifying a failure of the environment rather than of the agent.
# These are reported separately: counting a rate-limited run as a quality failure
# would quietly corrupt the pass rate.
_INFRASTRUCTURE_MARKERS = (
    "rate limit",
    "rate_limit_exceeded",
    "429",
    "connection",
    "timeout",
    "timed out",
    "temporarily unavailable",
    "service unavailable",
    "apiconnectionerror",
    # agents/_groq_client.py, once a transient provider failure outlasts
    # every retry. Non-retryable errors never carry this text.
    "groq request failed",
)


def is_infrastructure_error(message: str) -> bool:
    """True when a case failed because of the environment, not the agent."""
    lowered = str(message).lower()
    return any(marker in lowered for marker in _INFRASTRUCTURE_MARKERS)


async def _row_counts() -> Dict[str, int]:
    async with AsyncSessionLocal() as db:
        counts = {}
        for label, model in _COUNTED_TABLES.items():
            result = await db.execute(select(func.count()).select_from(model))
            counts[label] = int(result.scalar_one())
        return counts


def _answer_text(result: Dict[str, Any]) -> str:
    """Everything the agent said, for keyword expectations."""
    parts = [str(result.get("orchestrator_summary", ""))]
    parts += [str(a) for a in result.get("alerts", [])]
    parts += [str(a) for a in result.get("next_recommended_actions", [])]
    return "\n".join(parts)


def _claim_text(result: Dict[str, Any]) -> str:
    """
    Only the parts of the answer that assert something about retrieved data.

    ``next_recommended_actions`` is deliberately excluded. Those are, by the
    orchestrator's own contract, forward-looking proposals — "monitor over the
    next 24-48 hours", "pre-position an additional 10-15 units". Their numbers
    are planning horizons and suggested quantities, so by construction they
    cannot trace back to tool output, and feeding them to the grounding scorer
    manufactured false hallucination reports on otherwise-correct answers.

    Recommendations are not unpoliced: the LLM judge receives them in full and
    scores them for grounding and usefulness.
    """
    parts = [str(result.get("orchestrator_summary", ""))]
    parts += [str(a) for a in result.get("alerts", [])]
    return "\n".join(parts)


def _score_text_expectations(case: EvalCase, text: str) -> Dict[str, Any]:
    """Cheap keyword assertions, folded into the tool-selection dimension."""
    lowered = text.lower()
    missing = [m for m in case.must_mention if m.lower() not in lowered]
    present = [m for m in case.must_not_mention if m.lower() in lowered]
    problems = []
    if missing:
        problems.append(f"missing required mention {missing}")
    if present:
        problems.append(f"contains forbidden mention {present}")
    return {
        "passed": not problems,
        "detail": "; ".join(problems) or "text expectations met",
    }


def _metered(agent: Dict[str, int], judge: Optional[Dict[str, int]] = None) -> Dict[str, int]:
    """
    Every model call a case caused, split by who made it.

    The orchestrator's own ``usage`` covers its turns only. The judge, and tools
    that call a model (the report and comms agents), are counted here — they
    are real spend against the provider's daily limit, and a total that leaves
    them out understates what a run cost.
    """
    judge = judge or {"calls": 0, "total_tokens": 0}
    return {
        "agent_tokens": agent["total_tokens"],
        "judge_tokens": judge["total_tokens"],
        "total_tokens": agent["total_tokens"] + judge["total_tokens"],
        "calls": agent["calls"] + judge["calls"],
    }


async def run_case(case: EvalCase, *, use_judge: bool = True) -> Dict[str, Any]:
    """Run and score a single evaluation case."""
    from agents.orchestrator import run_orchestrator

    started = time.time()

    builder = SCENARIOS[case.scenario]
    truth = await builder()
    disaster_id = truth["disaster_id"]

    # The same briefing the agent gets, handed to the judge so the operation's
    # own name does not read as an invention.
    from agents.orchestrator import describe_disaster
    from database import crud

    async with AsyncSessionLocal() as db:
        disaster = await crud.get_disaster(db, disaster_id)
        operation_context = describe_disaster(disaster) if disaster else ""

    before = await _row_counts()

    async with AsyncSessionLocal() as db:
        # A run that fails partway has still spent tokens, so the meter wraps
        # the call and is read on every path out of this function.
        with track_usage() as agent_usage:
            try:
                result = await run_orchestrator(disaster_id, case.command, db)
            except Exception as exc:  # noqa: BLE001
                result = {"error": f"{type(exc).__name__}: {exc}"}

    if "error" in result and "trace" not in result:
        return {
            "case": case.name,
            "scenario": case.scenario,
            "tags": list(case.tags),
            "error": result["error"],
            "errored": is_infrastructure_error(result["error"]),
            "score": 0.0,
            "passed": False,
            "metered": _metered(agent_usage),
            "duration_s": round(time.time() - started, 1),
        }

    after = await _row_counts()

    trace = result.get("trace", [])
    tool_results = result.get("results", {})
    answer_text = _answer_text(result)

    # ── Dimensions ─────────────────────────────────────────────────────────
    dimensions: Dict[str, Dict[str, Any]] = {}

    selection = score_tool_selection(
        trace,
        required_tools=case.required_tools,
        forbidden_tools=case.forbidden_tools,
        any_of_tools=case.any_of_tools,
    )
    text_check = _score_text_expectations(case, answer_text)
    if not text_check["passed"]:
        selection = {
            **selection,
            "score": min(selection["score"], 0.5),
            "passed": False,
            "detail": f"{selection['detail']}; {text_check['detail']}",
        }
    dimensions["tool_selection"] = selection

    dimensions["grounding"] = score_grounding(_claim_text(result), tool_results)

    dimensions["side_effects"] = score_side_effects(
        before,
        after,
        expected_deltas=case.expected_deltas,
        forbid_other_writes=case.forbid_other_writes,
        allowed_extra_writes=case.allowed_extra_writes,
    )

    dimensions["loop_health"] = score_loop_health(
        result,
        max_iterations=case.max_iterations,
        max_tool_repeats=case.max_tool_repeats,
    )

    judge_usage = None
    if use_judge:
        with track_usage() as judge_usage:
            dimensions["judge"] = await score_judge(
                case.command,
                result,
                tool_results,
                case.expectation,
                operation_context=operation_context,
            )
    else:
        dimensions["judge"] = {
            "score": 1.0,
            "passed": True,
            "detail": "judge skipped (--no-judge)",
            "skipped": True,
        }

    # ── Weighted total ─────────────────────────────────────────────────────
    weights = dict(case.weights)
    if not use_judge:
        weights.pop("judge", None)
    total_weight = sum(weights.values()) or 1.0
    score = sum(dimensions[d]["score"] * w for d, w in weights.items()) / total_weight

    return {
        "case": case.name,
        "scenario": case.scenario,
        "tags": list(case.tags),
        "command": case.command,
        "score": round(score, 3),
        "passed": score >= PASS_THRESHOLD
        and all(dimensions[d]["passed"] for d in ("grounding", "side_effects")),
        "dimensions": dimensions,
        "tools_used": [t["tool"] for t in trace if t.get("step") == "tool"],
        "iterations": result.get("iterations"),
        "usage": result.get("usage", {}),
        "metered": _metered(agent_usage, judge_usage),
        "summary": str(result.get("orchestrator_summary", ""))[:400],
        "duration_s": round(time.time() - started, 1),
    }


def select_cases(
    names: Optional[Sequence[str]] = None,
    tags: Optional[Sequence[str]] = None,
    limit: Optional[int] = None,
) -> List[EvalCase]:
    cases = list(CASES)
    if names:
        missing = [n for n in names if n not in CASES_BY_NAME]
        if missing:
            raise SystemExit(f"Unknown case(s): {', '.join(missing)}")
        cases = [CASES_BY_NAME[n] for n in names]
    if tags:
        wanted = set(tags)
        cases = [c for c in cases if wanted & set(c.tags)]
    if limit:
        cases = cases[:limit]
    return cases


async def run_suite(
    cases: Sequence[EvalCase],
    *,
    use_judge: bool = True,
    delay_s: float = 0.0,
    on_result=None,
) -> Dict[str, Any]:
    """
    Run every case sequentially and aggregate the results.

    ``delay_s`` paces the suite between cases. Groq's free tier enforces a
    tokens-per-minute budget that a full run will otherwise saturate, sending
    every call into exponential backoff — pacing is usually faster overall than
    fighting the rate limiter.
    """
    await create_all_tables()

    started = time.time()
    results: List[Dict[str, Any]] = []

    aborted = False
    for index, case in enumerate(cases):
        if index and delay_s:
            await asyncio.sleep(delay_s)
        result = await run_case(case, use_judge=use_judge)
        results.append(result)
        if on_result:
            on_result(result)

        # Once the daily token budget is gone every remaining case would report
        # the same environment failure, so stop rather than manufacture noise.
        if result.get("errored") and "per day" in str(result.get("error", "")):
            aborted = True
            break

    errored = [r for r in results if r.get("errored")]
    assessable = [r for r in results if not r.get("errored")]
    passed = [r for r in assessable if r["passed"]]
    scored = [r for r in results if "dimensions" in r]

    dimension_means: Dict[str, float] = {}
    for name in ("tool_selection", "grounding", "side_effects", "loop_health", "judge"):
        values = [r["dimensions"][name]["score"] for r in scored if name in r["dimensions"]]
        if values:
            dimension_means[name] = round(sum(values) / len(values), 3)

    # The orchestrator's own turns — kept under its old meaning so reports stay
    # comparable with earlier runs. metered_tokens is what the run really spent.
    total_tokens = sum(r.get("usage", {}).get("total_tokens", 0) for r in results)
    metered_tokens = sum(r.get("metered", {}).get("total_tokens", 0) for r in results)
    total_cost = sum(
        r.get("usage", {}).get("estimated_cost_usd") or 0.0 for r in results
    )

    return {
        "total": len(results),
        "requested": len(cases),
        "passed": len(passed),
        "failed": len(assessable) - len(passed),
        "errored": len(errored),
        "aborted": aborted,
        "pass_rate": (
            round(len(passed) / len(assessable), 3) if assessable else 0.0
        ),
        "mean_score": (
            round(sum(r["score"] for r in assessable) / len(assessable), 3)
            if assessable
            else 0.0
        ),
        "dimension_means": dimension_means,
        "total_tokens": total_tokens,
        "metered_tokens": metered_tokens,
        "estimated_cost_usd": round(total_cost, 6),
        "duration_s": round(time.time() - started, 1),
        "results": results,
    }


def format_report(report: Dict[str, Any], verbose: bool = False) -> str:
    """Render a run as a terminal report."""
    lines: List[str] = []
    width = 78

    lines.append("=" * width)
    lines.append("ReliefAI evaluation report")
    lines.append("=" * width)
    lines.append("")

    for r in report["results"]:
        if r.get("errored"):
            lines.append(f"[ERR ] {r['case']}  —  environment: {str(r['error'])[:150]}")
            lines.append("")
            continue

        mark = "PASS" if r["passed"] else "FAIL"
        if "error" in r:
            lines.append(f"[{mark}] {r['case']}  —  ERROR: {r['error']}")
            lines.append("")
            continue

        lines.append(f"[{mark}] {r['case']}  ({r['score']:.2f})  {r['duration_s']}s")
        lines.append(f"       tools: {r['tools_used'] or '—'}")

        for name, dim in r["dimensions"].items():
            flag = "ok " if dim["passed"] else "!! "
            lines.append(f"       {flag}{name:<15} {dim['score']:.2f}  {dim['detail'][:110]}")

        if verbose:
            lines.append(f"       summary: {r['summary'][:200]}")
        lines.append("")

    lines.append("-" * width)
    assessable = report["total"] - report.get("errored", 0)
    lines.append(
        f"{report['passed']}/{assessable} assessable cases passed "
        f"({report['pass_rate'] * 100:.0f}%)   mean score {report['mean_score']:.2f}"
    )
    if report.get("errored"):
        lines.append(
            f"{report['errored']} case(s) could not be assessed "
            "(environment failure, excluded from the pass rate)"
        )
    if report.get("aborted"):
        lines.append(
            f"RUN ABORTED — {report['requested'] - report['total']} case(s) not "
            "attempted. The provider's daily token budget was exhausted."
        )
    lines.append("")
    lines.append("Dimension means:")
    for name, value in report["dimension_means"].items():
        bar = "█" * int(round(value * 20))
        lines.append(f"  {name:<16} {value:.2f}  {bar}")
    lines.append("")
    lines.append(
        f"{report.get('metered_tokens', 0):,} tokens metered across every model "
        f"call ({report['total_tokens']:,} in orchestrator turns; the rest is "
        "the judge and tools that call a model)"
    )
    lines.append(
        f"~${report['estimated_cost_usd']:.4f} excluding the judge   "
        f"{report['duration_s']}s"
    )
    lines.append("=" * width)
    return "\n".join(lines)
