"""
Scoring dimensions for ReliefAI evaluations.

Four scorers are fully deterministic — they read the execution trace and the
database, so they cost nothing and never flake. The fifth is an LLM judge, used
only for the qualitative dimension the others genuinely cannot cover.

Every scorer returns::

    {"score": 0.0-1.0, "passed": bool, "detail": str, ...extras}
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from agents._groq_client import _call_groq

# A claimed number is treated as grounded if it is within this relative distance
# of a value the tools actually returned (covers rounding and unit prose).
_NUMERIC_TOLERANCE = 0.005

# A leading "-" counts as a minus sign only when it does not follow a digit or a
# decimal point. Without the lookbehind the hyphens in an ISO-8601 timestamp read
# as negation: "2026-08-30" yields -8 and -30, so a date the agent correctly
# echoes back in prose ("due Aug 30 at 08:00" -> +8, +30) fails to match the
# tool output it came from and is reported as a hallucination.
_NUMBER_RE = re.compile(r"(?<![\d.])-?\d+(?:\.\d+)?")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tools_used(trace: Sequence[Dict[str, Any]]) -> List[str]:
    return [t["tool"] for t in trace if t.get("step") == "tool"]


def _iter_numbers(value: Any) -> Iterable[float]:
    """Every numeric value appearing anywhere in a nested structure."""
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        yield float(value)
    elif isinstance(value, dict):
        for v in value.values():
            yield from _iter_numbers(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _iter_numbers(v)
    elif isinstance(value, str):
        for match in _NUMBER_RE.findall(value):
            try:
                yield float(match)
            except ValueError:
                continue


def _record_numbers(value: Any) -> Iterable[List[float]]:
    """Groups of numbers that share a record, so differences can be derived."""
    if isinstance(value, dict):
        own = [
            float(v)
            for v in value.values()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        ]
        if own:
            yield own
        for v in value.values():
            yield from _record_numbers(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _record_numbers(v)


def _grounded_number_set(tool_results: Any) -> Set[float]:
    """
    Values the agent may legitimately state: everything the tools returned, plus
    within-record differences and percentages, which is how a real shortfall
    ("needs 2000, has 400, short 1600") gets computed.
    """
    numbers: Set[float] = set(_iter_numbers(tool_results))

    for group in _record_numbers(tool_results):
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                numbers.add(abs(a - b))
                numbers.add(a + b)
                if b:
                    numbers.add(round(a / b * 100, 1))
                    numbers.add(round(a / b * 100))
                if a:
                    numbers.add(round(b / a * 100, 1))
                    numbers.add(round(b / a * 100))

    # Counts of returned collections are legitimate claims too.
    if isinstance(tool_results, dict):
        for v in tool_results.values():
            if isinstance(v, list):
                numbers.add(float(len(v)))

    return numbers


def _claimed_numbers(text: str) -> List[float]:
    """
    Numbers asserted in prose, with thousands separators and the narrow/no-break
    spaces some models emit inside numerals normalised away.

    Only bare small integers are excluded, as list numbering ("top 3 issues")
    rather than factual claims. Years are deliberately *not* excluded: dates in
    tool output are ISO strings, so a legitimately cited year is already in the
    grounded set, whereas excluding the range would blind the scorer to real
    quantities that fall in it — 2000 liters, for instance.
    """
    cleaned = text.replace(" ", "").replace(" ", "")
    cleaned = re.sub(r"(?<=\d),(?=\d{3}\b)", "", cleaned)

    out: List[float] = []
    for raw in _NUMBER_RE.findall(cleaned):
        try:
            value = float(raw)
        except ValueError:
            continue
        if value.is_integer() and abs(value) <= 3:
            continue
        out.append(value)
    return out


def _is_grounded(value: float, allowed: Set[float]) -> bool:
    if value in allowed:
        return True
    for candidate in allowed:
        if candidate == 0:
            if abs(value) < 1e-9:
                return True
            continue
        if abs(value - candidate) / abs(candidate) <= _NUMERIC_TOLERANCE:
            return True
    return False


# ---------------------------------------------------------------------------
# 1. Tool selection
# ---------------------------------------------------------------------------


def score_tool_selection(
    trace: Sequence[Dict[str, Any]],
    required_tools: Sequence[str] = (),
    forbidden_tools: Sequence[str] = (),
    any_of_tools: Sequence[str] = (),
) -> Dict[str, Any]:
    """Did the model reach for the right tools, and avoid the wrong ones?"""
    used = _tools_used(trace)
    used_set = set(used)

    missing = [t for t in required_tools if t not in used_set]
    forbidden_used = [t for t in forbidden_tools if t in used_set]
    any_of_satisfied = not any_of_tools or bool(used_set & set(any_of_tools))

    checks = len(required_tools) + len(forbidden_tools) + (1 if any_of_tools else 0)
    failures = len(missing) + len(forbidden_used) + (0 if any_of_satisfied else 1)
    score = 1.0 if checks == 0 else max(0.0, (checks - failures) / checks)

    details = []
    if missing:
        details.append(f"missing {missing}")
    if forbidden_used:
        details.append(f"called forbidden {forbidden_used}")
    if not any_of_satisfied:
        details.append(f"called none of {list(any_of_tools)}")

    return {
        "score": score,
        "passed": failures == 0,
        "detail": "; ".join(details) or f"used {used}",
        "tools_used": used,
    }


# ---------------------------------------------------------------------------
# 2. Grounding — are the stated numbers real?
# ---------------------------------------------------------------------------


def score_grounding(answer_text: str, tool_results: Any) -> Dict[str, Any]:
    """
    Check every number the agent states against what the tools returned.

    This is the automated hallucination check: an agent that invents a stock
    level or a volunteer count fails here even when the prose reads well.
    """
    claimed = _claimed_numbers(answer_text)
    if not claimed:
        return {
            "score": 1.0,
            "passed": True,
            "detail": "no numeric claims to verify",
            "unverified": [],
        }

    allowed = _grounded_number_set(tool_results)
    unverified = [n for n in claimed if not _is_grounded(n, allowed)]
    score = (len(claimed) - len(unverified)) / len(claimed)

    return {
        "score": score,
        "passed": not unverified,
        "detail": (
            f"{len(claimed) - len(unverified)}/{len(claimed)} numeric claims "
            f"traced to tool output"
            + (f"; unverified: {unverified}" if unverified else "")
        ),
        "unverified": unverified,
    }


# ---------------------------------------------------------------------------
# 3. Side effects — did the right writes happen, and only those?
# ---------------------------------------------------------------------------


def score_side_effects(
    before: Dict[str, int],
    after: Dict[str, int],
    expected_deltas: Optional[Dict[str, int]] = None,
    forbid_other_writes: bool = True,
) -> Dict[str, Any]:
    """
    Compare database row counts before and after the run.

    ``expected_deltas`` gives the minimum change per table (e.g. ``{"tasks": 1}``).
    With ``forbid_other_writes`` every unlisted table must be unchanged, which is
    how read-only commands are held to being read-only.
    """
    expected_deltas = expected_deltas or {}
    actual = {k: after.get(k, 0) - before.get(k, 0) for k in set(before) | set(after)}

    problems: List[str] = []
    checks = 0

    for table, minimum in expected_deltas.items():
        checks += 1
        got = actual.get(table, 0)
        if got < minimum:
            problems.append(f"{table}: expected >= {minimum}, got {got}")

    if forbid_other_writes:
        for table, delta in actual.items():
            if table in expected_deltas:
                continue
            checks += 1
            if delta != 0:
                problems.append(f"{table}: unexpected change of {delta:+d}")

    score = 1.0 if checks == 0 else max(0.0, (checks - len(problems)) / checks)

    return {
        "score": score,
        "passed": not problems,
        "detail": "; ".join(problems) or f"deltas {actual}",
        "deltas": actual,
    }


# ---------------------------------------------------------------------------
# 4. Loop health — did the agent behave efficiently and without errors?
# ---------------------------------------------------------------------------


def score_loop_health(
    result: Dict[str, Any],
    max_iterations: int = 5,
    allow_tool_errors: bool = False,
) -> Dict[str, Any]:
    """Penalise failed tool calls, runaway loops, and repeated identical calls."""
    trace = result.get("trace", [])
    tool_steps = [t for t in trace if t.get("step") == "tool"]
    failed = [t for t in tool_steps if not t.get("ok")]
    repeats = [t for t in failed if "Repeated call" in str(t.get("error", ""))]
    iterations = result.get("iterations", 0)

    problems: List[str] = []
    if failed and not allow_tool_errors:
        problems.append(f"{len(failed)} failed tool call(s)")
    if repeats:
        problems.append(f"{len(repeats)} duplicate call(s) refused")
    if iterations > max_iterations:
        problems.append(f"{iterations} iterations exceeds budget of {max_iterations}")
    if not tool_steps:
        problems.append("no tools were called at all")

    checks = 4
    score = max(0.0, (checks - len(problems)) / checks)

    return {
        "score": score,
        "passed": not problems,
        "detail": (
            "; ".join(problems)
            or f"{len(tool_steps)} tool call(s) in {iterations} iteration(s), no errors"
        ),
        "iterations": iterations,
        "tool_calls": len(tool_steps),
    }


# ---------------------------------------------------------------------------
# 5. LLM judge — the qualitative dimension
# ---------------------------------------------------------------------------

_JUDGE_SYSTEM = (
    "You are a strict evaluator of an AI disaster-relief coordination agent. "
    "You are given an operator's command, the raw data the agent's tools "
    "returned, and the agent's final answer.\n\n"
    "Score each criterion from 1 to 5:\n"
    "  relevance   — does the answer actually address the command?\n"
    "  grounding   — is every claim supported by the tool data? Any claim that "
    "is not supported must score 2 or below.\n"
    "  usefulness  — would a relief coordinator be able to act on this?\n\n"
    "Be harsh. A fluent answer that states unsupported facts is a bad answer. "
    "An answer that correctly reports 'no problems found' when the data shows "
    "none is a GOOD answer and must not be penalised for being unalarming.\n\n"
    "Respond with ONLY valid JSON: {\"relevance\": int, \"grounding\": int, "
    "\"usefulness\": int, \"reasoning\": str, \"unsupported_claims\": [str]}"
)


async def score_judge(
    command: str,
    answer: Dict[str, Any],
    tool_results: Any,
    expectation: str = "",
) -> Dict[str, Any]:
    """LLM-as-judge over relevance, grounding and operational usefulness."""
    tool_blob = json.dumps(tool_results, default=str)
    if len(tool_blob) > 6000:
        tool_blob = tool_blob[:6000] + "…[truncated]"

    answer_blob = json.dumps(
        {
            "orchestrator_summary": answer.get("orchestrator_summary", ""),
            "alerts": answer.get("alerts", []),
            "next_recommended_actions": answer.get("next_recommended_actions", []),
        },
        default=str,
    )

    user_message = (
        f"OPERATOR COMMAND:\n{command}\n\n"
        f"TOOL DATA THE AGENT RETRIEVED:\n{tool_blob}\n\n"
        f"AGENT'S FINAL ANSWER:\n{answer_blob}\n"
    )
    if expectation:
        user_message += f"\nWHAT A CORRECT ANSWER SHOULD DO:\n{expectation}\n"

    try:
        verdict = await _call_groq(_JUDGE_SYSTEM, user_message)
    except Exception as exc:  # noqa: BLE001
        return {
            "score": 0.0,
            "passed": False,
            "detail": f"judge unavailable: {exc}",
            "error": str(exc),
        }

    def _rating(key: str) -> int:
        value = verdict.get(key, 0)
        try:
            return max(1, min(5, int(value)))
        except (TypeError, ValueError):
            return 1

    relevance = _rating("relevance")
    grounding = _rating("grounding")
    usefulness = _rating("usefulness")
    mean = (relevance + grounding + usefulness) / 3.0

    return {
        "score": (mean - 1) / 4.0,  # map 1-5 onto 0-1
        "passed": mean >= 4.0 and grounding >= 4,
        "detail": (
            f"relevance={relevance} grounding={grounding} usefulness={usefulness} "
            f"— {str(verdict.get('reasoning', ''))[:200]}"
        ),
        "ratings": {
            "relevance": relevance,
            "grounding": grounding,
            "usefulness": usefulness,
        },
        "unsupported_claims": verdict.get("unsupported_claims", []),
    }
