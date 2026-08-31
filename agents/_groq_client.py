"""
Shared Groq call helpers.  Import from here — do NOT duplicate.

Two entry points:

  ``_call_groq``            single-shot JSON-mode call (structured output, no tools)
  ``_call_groq_with_tools`` native function-calling turn used by the orchestrator
                            loop; returns the raw assistant message so the caller
                            can inspect ``tool_calls``

Both retry with exponential backoff on rate limit errors (429).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any, Dict, List, Optional

from groq import AsyncGroq, RateLimitError

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

# Max retries and base delay (seconds) for exponential backoff
_MAX_RETRIES = 4
_BASE_DELAY  = 5.0


async def _call_groq(system_prompt: str, user_message: str) -> dict:
    """
    Call Groq chat completions with JSON-mode enabled.
    Automatically retries on 429 rate-limit errors with exponential backoff:
      Attempt 1 → wait 5s
      Attempt 2 → wait 10s
      Attempt 3 → wait 20s
      Attempt 4 → wait 40s (or Groq's suggested wait, whichever is longer)
    Raises RuntimeError after all retries exhausted.
    Non-rate-limit errors propagate immediately.
    """
    client = AsyncGroq(api_key=os.environ["GROQ_API_KEY"])
    last_exc: Exception | None = None

    for attempt in range(_MAX_RETRIES):
        try:
            response = await client.chat.completions.create(
                model=_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_message},
                ],
                temperature=0.3,
                max_tokens=4096,
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content
            return json.loads(raw)

        except RateLimitError as exc:
            last_exc = exc
            # Exponential backoff: 5, 10, 20, 40 seconds
            wait = _BASE_DELAY * (2 ** attempt)
            # Respect Groq's suggested retry-after if embedded in error message
            m = re.search(r'try again in (\d+(?:\.\d+)?)s', str(exc))
            if m:
                suggested = float(m.group(1))
                # Use whichever is longer, capped at 60s
                wait = min(max(wait, suggested + 2), 60)

            if attempt < _MAX_RETRIES - 1:
                # More attempts remaining — wait and retry
                await asyncio.sleep(wait)
                continue

            # All retries exhausted
            raise RuntimeError(
                f"Groq rate limit exceeded after {_MAX_RETRIES} attempts. "
                f"You may have hit the daily token limit (100k/day on free tier). "
                f"Resets at midnight UTC. Original error: {exc}"
            ) from exc

        except Exception:
            # Network errors, parse errors, etc. — don't retry
            raise

    # Unreachable, but satisfies type checker
    if last_exc:
        raise last_exc

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
    loop. Retries on 429 with the same backoff policy.

    Returns::

        {
          "message": <assistant message dict, may contain tool_calls>,
          "finish_reason": str,
          "usage": {"input_tokens": int, "output_tokens": int, "total_tokens": int},
          "latency_ms": int,
        }
    """
    client = AsyncGroq(api_key=os.environ["GROQ_API_KEY"])
    last_exc: Exception | None = None

    for attempt in range(_MAX_RETRIES):
        started = asyncio.get_event_loop().time()
        try:
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

            response = await client.chat.completions.create(**kwargs)
            latency_ms = int((asyncio.get_event_loop().time() - started) * 1000)

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

            usage = response.usage
            return {
                "message": {
                    "role": "assistant",
                    "content": message.content,
                    "tool_calls": tool_calls or None,
                },
                "finish_reason": choice.finish_reason,
                "usage": {
                    "input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                    "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
                    "total_tokens": getattr(usage, "total_tokens", 0) or 0,
                },
                "latency_ms": latency_ms,
            }

        except RateLimitError as exc:
            last_exc = exc
            wait = _BASE_DELAY * (2 ** attempt)
            m = re.search(r"try again in (\d+(?:\.\d+)?)s", str(exc))
            if m:
                wait = min(max(wait, float(m.group(1)) + 2), 60)
            if attempt < _MAX_RETRIES - 1:
                await asyncio.sleep(wait)
                continue
            raise RuntimeError(
                f"Groq rate limit exceeded after {_MAX_RETRIES} attempts. "
                f"Original error: {exc}"
            ) from exc

        except Exception:
            raise

    if last_exc:
        raise last_exc
    raise RuntimeError("Groq tool call failed for an unknown reason.")
