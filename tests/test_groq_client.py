"""
Tests for the shared Groq client: one retry policy, and every call metered.

The SDK client is scripted (``fake_groq`` in conftest), so the retry loop and
the usage meters run for real with no network access.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from groq import (
    APIConnectionError,
    BadRequestError,
    InternalServerError,
    RateLimitError,
)

from agents import _groq_client as gc
from tests.conftest import GROQ_REQUEST, groq_response

PER_MINUTE = (
    "Rate limit reached for model `openai/gpt-oss-120b` on tokens per minute "
    "(TPM): Limit 8000, Used 7900, Requested 2000."
)
PER_DAY = (
    "Rate limit reached for model `openai/gpt-oss-120b` on tokens per day "
    "(TPD): Limit 200000, Used 199500, Requested 2000. Please try again in 7m12s."
)


def _rate_limited(message: str = PER_MINUTE) -> RateLimitError:
    return RateLimitError(
        message, response=httpx.Response(429, request=GROQ_REQUEST), body=None
    )


def _server_error() -> InternalServerError:
    return InternalServerError(
        "upstream failure",
        response=httpx.Response(500, request=GROQ_REQUEST),
        body=None,
    )


def _bad_request() -> BadRequestError:
    return BadRequestError(
        "tool schema rejected",
        response=httpx.Response(400, request=GROQ_REQUEST),
        body=None,
    )


async def _turn():
    return await gc._call_groq_with_tools([{"role": "user", "content": "hi"}], [])


# ---------------------------------------------------------------------------
# One retry policy
# ---------------------------------------------------------------------------


async def test_sdk_retries_are_switched_off(fake_groq):
    """The SDK's own retries ran inside each of ours — up to twelve requests."""
    state = fake_groq(groq_response('{"ok": true}'))
    await gc._call_groq("system", "user")
    assert state["clients"][0]["max_retries"] == 0


async def test_rate_limit_is_retried_and_attempts_are_reported(fake_groq):
    state = fake_groq(_rate_limited(), _rate_limited(), groq_response("done"))
    turn = await _turn()
    assert len(state["requests"]) == 3
    assert turn["attempts"] == 3
    assert turn["message"]["content"] == "done"


async def test_transient_server_and_connection_errors_are_retried(fake_groq):
    state = fake_groq(
        _server_error(), APIConnectionError(request=GROQ_REQUEST), groq_response("ok")
    )
    turn = await _turn()
    assert turn["attempts"] == 3
    assert len(state["requests"]) == 3


async def test_a_malformed_request_is_not_retried_and_propagates_unchanged(fake_groq):
    """A 400 is our bug, not the provider's weather; retrying cannot fix it."""
    state = fake_groq(_bad_request(), groq_response("never reached"))
    with pytest.raises(BadRequestError):
        await _turn()
    assert len(state["requests"]) == 1


async def test_a_daily_limit_is_not_retried(fake_groq):
    """The daily budget does not come back inside a 35-second backoff."""
    state = fake_groq(_rate_limited(PER_DAY), groq_response("never reached"))
    with pytest.raises(RateLimitError) as raised:
        await _turn()
    assert len(state["requests"]) == 1
    # The runner aborts the suite on this phrase, so it must survive.
    assert "per day" in str(raised.value)


async def test_an_exhausted_per_minute_limit_does_not_read_as_daily(fake_groq):
    """The runner aborts a suite on "per day"; a TPM 429 must not trip that."""
    state = fake_groq(*[_rate_limited() for _ in range(gc._MAX_ATTEMPTS)])
    with pytest.raises(RuntimeError) as raised:
        await _turn()
    message = str(raised.value).lower()
    assert len(state["requests"]) == gc._MAX_ATTEMPTS
    assert "rate limit" in message
    assert "per day" not in message
    assert "100k" not in message


async def test_an_exhausted_transient_failure_is_an_environment_failure(fake_groq):
    from evals.runner import is_infrastructure_error

    fake_groq(*[_server_error() for _ in range(gc._MAX_ATTEMPTS)])
    with pytest.raises(RuntimeError) as raised:
        await _turn()
    assert is_infrastructure_error(str(raised.value))


def test_non_retryable_errors_are_not_classified_as_environment():
    """Only the exhaustion wrapper carries the marker; a raw 400 does not."""
    from evals.runner import is_infrastructure_error

    assert not is_infrastructure_error(str(_bad_request()))


def test_backoff_honours_the_providers_hint_within_a_cap():
    hinted = _rate_limited(PER_MINUTE + " Please try again in 12.5s.")
    assert gc._retry_wait(hinted, 0) == pytest.approx(14.5)
    patient = _rate_limited(PER_MINUTE + " Please try again in 300s.")
    assert gc._retry_wait(patient, 0) == 60


# ---------------------------------------------------------------------------
# Usage metering
# ---------------------------------------------------------------------------


async def test_a_meter_counts_both_entry_points(fake_groq):
    fake_groq(
        groq_response('{"a": 1}', usage=(10, 5)),
        groq_response("turn", usage=(7, 3)),
    )
    with gc.track_usage() as meter:
        await gc._call_groq("system", "user")
        await _turn()
    assert meter == {
        "calls": 2,
        "input_tokens": 17,
        "output_tokens": 8,
        "total_tokens": 25,
    }


async def test_nested_meters_both_see_a_call_and_close_cleanly(fake_groq):
    fake_groq(groq_response("inner", usage=(4, 1)), groq_response("outer", usage=(2, 2)))
    with gc.track_usage() as outer:
        with gc.track_usage() as inner:
            await _turn()
        await _turn()
    assert inner["total_tokens"] == 5
    assert outer["total_tokens"] == 9


async def test_meters_follow_concurrent_tasks(fake_groq):
    """Tool calls run under asyncio.gather; their model calls must still count."""
    fake_groq(groq_response('{"a": 1}'), groq_response('{"b": 2}'))
    with gc.track_usage() as meter:
        await asyncio.gather(gc._call_groq("s", "u"), gc._call_groq("s", "u"))
    assert meter["calls"] == 2


async def test_failed_attempts_are_not_metered(fake_groq):
    """A 429 consumes no tokens; only the attempt that answered is billed."""
    fake_groq(_rate_limited(), groq_response("ok", usage=(10, 5)))
    with gc.track_usage() as meter:
        await _turn()
    assert meter["calls"] == 1
    assert meter["total_tokens"] == 15


async def test_calls_outside_any_meter_still_work(fake_groq):
    fake_groq(groq_response('{"x": 1}'))
    assert await gc._call_groq("s", "u") == {"x": 1}
