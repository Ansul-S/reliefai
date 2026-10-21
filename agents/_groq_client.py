"""
Shared Groq call helpers.  Import from here — do NOT duplicate.

Two entry points:

  ``_call_groq``            single-shot JSON-mode call (structured output, no tools)
  ``_call_groq_with_tools`` native function-calling turn used by the orchestrator
                            loop; returns the raw assistant message so the caller
                            can inspect ``tool_calls``

Both go through ``_create``, the one place that retries. Every successful call
is metered twice: into the daily spend cap (``ratelimit``) and into any
``track_usage()`` block open around it.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Iterator, List, Optional, Tuple

from groq import APIConnectionError, APIStatusError, AsyncGroq, RateLimitError

_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")

# Approximate Groq pricing (USD per 1M tokens) for cost estimation in traces.
# Unknown models fall back to no cost estimate rather than a wrong one.
_PRICING_PER_MTOK = {
    "openai/gpt-oss-120b": {"input": 0.15, "output": 0.75},
    "openai/gpt-oss-20b": {"input": 0.10, "output": 0.50},
}


def get_model_name() -> str:
    """The Groq model id currently in use."""
    return _MODEL


def estimate_cost_usd(input_tokens: int, output_tokens: int) -> Optional[float]:
    """Estimated USD cost for a call, or None if the model has no known pricing."""
    pricing = _PRICING_PER_MTOK.get(_MODEL)
    if pricing is None:
        return None
    return round(
        (input_tokens / 1_000_000) * pricing["input"]
        + (output_tokens / 1_000_000) * pricing["output"],
        8,
    )

# Attempts per logical call, and the base delay (seconds) for exponential
# backoff between them: 5, 10, 20.
_MAX_ATTEMPTS = 4
_BASE_DELAY = 5.0


# ---------------------------------------------------------------------------
# Usage metering
# ---------------------------------------------------------------------------

# Every meter open in the current context. A tuple, so an inner block (one tool
# call) and an outer one (a whole eval case) both see the same model call.
_usage_meters: ContextVar[Tuple[Dict[str, int], ...]] = ContextVar(
    "_usage_meters", default=()
)


@contextmanager
def track_usage() -> Iterator[Dict[str, int]]:
    """
    Meter every model call made inside the block, however deeply nested.

    The orchestrator's ``usage`` counts its own turns only. A tool that calls a
    model — the report and comms agents — and the eval judge are invisible to
    it, so any total summed from those blocks undercounts what Groq bills. The
    meter rides on the context rather than on call signatures: asyncio tasks
    copy the context, and the dicts inside it are shared, so tool calls running
    concurrently report into the same meters.
    """
    meter = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    token = _usage_meters.set(_usage_meters.get() + (meter,))
    try:
        yield meter
    finally:
        _usage_meters.reset(token)


def _report_tokens(count: int) -> None:
    """
    Tell the rate limiter what a call actually cost.

    Reporting here rather than in HTTP middleware means every call is counted —
    the orchestrator loop, the specialist agents and the eval judge alike — and
    the daily spend cap reflects real consumption rather than a guess derived
    from request counts.

    Deliberately best-effort: a metering failure must never break a model call.
    """
    try:
        import ratelimit

        ratelimit.record_tokens(count)
    except Exception:
        pass


def _record_usage(usage: Any) -> Dict[str, int]:
    """Normalise a response's usage block and report it to every meter."""
    counts = {
        "input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
        "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
        "total_tokens": getattr(usage, "total_tokens", 0) or 0,
    }
    for meter in _usage_meters.get():
        meter["calls"] += 1
        for key, value in counts.items():
            meter[key] += value
    _report_tokens(counts["total_tokens"])
    return counts


# ---------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------


def _is_retryable(exc: Exception) -> bool:
    """Rate limits and transient provider failures; never a malformed request."""
    if isinstance(exc, RateLimitError):
        # A daily limit does not come back within this backoff schedule, so
        # retrying only delays the failure by half a minute.
        return "per day" not in str(exc).lower()
    if isinstance(exc, APIConnectionError):  # includes APITimeoutError
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code >= 500 or exc.status_code in (408, 409)
    return False


def _retry_wait(exc: Exception, attempt: int) -> float:
    """Exponential backoff, stretched to Groq's own suggestion when it gives one."""
    wait = _BASE_DELAY * (2 ** attempt)
    m = re.search(r"try again in (\d+(?:\.\d+)?)s", str(exc))
    if m:
        wait = min(max(wait, float(m.group(1)) + 2), 60)
    return wait


def _exhausted(exc: Exception, attempts: int) -> RuntimeError:
    """
    The error raised when a retryable failure outlasts every attempt.

    The runner classifies failures by substring: "rate limit" and "groq request
    failed" mark an environment failure, and "per day" aborts the suite. The
    provider's own text is kept for that reason, and this wrapper never adds
    "per day" itself — a per-minute 429 that outlasts the retries must not read
    as a daily one.
    """
    if isinstance(exc, RateLimitError):
        return RuntimeError(
            f"Groq rate limit exceeded after {attempts} attempt(s). "
            f"Original error: {exc}"
        )
    return RuntimeError(
        f"Groq request failed after {attempts} attempt(s) "
        f"({type(exc).__name__}). Original error: {exc}"
    )


async def _create(**kwargs: Any) -> Tuple[Any, Dict[str, int]]:
    """
    One logical chat completion, retried here and only here.

    The SDK's own retries are switched off (``max_retries=0``). Left on, its
    two retries ran inside each of these attempts — up to twelve requests per
    call into an already rate-limited bucket — and their waits landed inside
    the measured latency, so a slow-looking turn could really be backoff.

    Returns ``(response, timing)`` where timing is ``{attempts, retry_wait_ms,
    latency_ms}`` and ``latency_ms`` covers the successful attempt only.
    """
    client = AsyncGroq(api_key=os.environ["GROQ_API_KEY"], max_retries=0)
    waited = 0.0

    for attempt in range(_MAX_ATTEMPTS):
        started = time.monotonic()
        try:
            response = await client.chat.completions.create(**kwargs)
        except Exception as exc:
            # Anything not worth retrying — a malformed request, bad auth, a
            # daily limit — propagates unchanged, as it always has.
            if not _is_retryable(exc):
                raise
            if attempt == _MAX_ATTEMPTS - 1:
                raise _exhausted(exc, attempt + 1) from exc
            wait = _retry_wait(exc, attempt)
            waited += wait
            await asyncio.sleep(wait)
            continue

        return response, {
            "attempts": attempt + 1,
            "retry_wait_ms": int(waited * 1000),
            "latency_ms": int((time.monotonic() - started) * 1000),
        }

    raise RuntimeError("Groq call failed for an unknown reason.")


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


async def _call_groq(system_prompt: str, user_message: str) -> dict:
    """
    Call Groq chat completions with JSON-mode enabled and return the parsed
    object. Retries are handled by ``_create``; anything it gives up on is
    raised as ``RuntimeError``.
    """
    response, _timing = await _create(
        model=_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        temperature=0.3,
        max_tokens=4096,
        response_format={"type": "json_object"},
    )
    _record_usage(response.usage)
    return json.loads(response.choices[0].message.content)


async def _call_groq_with_tools(
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    *,
    tool_choice: str = "auto",
    response_format: Optional[Dict[str, Any]] = None,
    temperature: float = 0.3,
    max_tokens: int = 4096,
) -> Dict[str, Any]:
    """
    Run one native function-calling turn against Groq.

    Unlike :func:`_call_groq` this does not parse JSON — it hands back the raw
    assistant message so the caller can inspect ``tool_calls`` and drive the
    loop.

    Returns::

        {
          "message": <assistant message dict, may contain tool_calls>,
          "finish_reason": str,
          "usage": {"input_tokens": int, "output_tokens": int, "total_tokens": int},
          "latency_ms": int,       # the successful attempt only
          "attempts": int,
          "retry_wait_ms": int,    # time spent backing off before it
        }
    """
    kwargs: Dict[str, Any] = {
        "model": _MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = tool_choice
    if response_format is not None:
        kwargs["response_format"] = response_format

    response, timing = await _create(**kwargs)

    choice = response.choices[0]
    message = choice.message

    tool_calls = []
    for tc in (message.tool_calls or []):
        tool_calls.append(
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
        )

    return {
        "message": {
            "role": "assistant",
            "content": message.content,
            "tool_calls": tool_calls or None,
        },
        "finish_reason": choice.finish_reason,
        "usage": _record_usage(response.usage),
        **timing,
    }
