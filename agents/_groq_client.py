"""
Shared Groq call helper.  Import _call_groq from here — do NOT duplicate.
Includes automatic retry with exponential backoff for rate limit errors (429).
"""
from __future__ import annotations

import asyncio
import json
import os
import re

from groq import AsyncGroq, RateLimitError

_MODEL = "llama-3.3-70b-versatile"

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