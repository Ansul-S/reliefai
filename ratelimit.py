"""
Rate limiting and a spend circuit breaker.

Two different attacks, two different defences:

  Per-IP throttling stops one client hammering the API. It is the obvious
  layer, and on its own it is not enough — a bot with a hundred addresses slips
  straight through it.

  A global daily cap on model-invoking calls is the one that actually protects
  the thing worth protecting. The scarce resource here is not CPU, it is the
  Groq free tier's 200,000 tokens per day: once those are gone every agent call
  fails and the demo is dead for everyone, including whoever you wanted to show
  it to. The cap trips before the budget does, and it trips regardless of how
  many addresses the traffic came from.

State is in memory. That is a deliberate fit to the deployment rather than an
oversight: Render's free tier runs a single instance that sleeps when idle, so
there is nowhere to put a shared counter and no second replica to share it
with. The consequences are honest ones — counters reset when the instance
restarts, and this would need Redis the moment a second replica exists.
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Deque, Dict, Tuple

# ── Tiers ──────────────────────────────────────────────────────────────────
# Named by what a request costs, matching how the API gates authentication.

LLM = "llm"
WRITE = "write"
READ = "read"


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


# Per-IP allowances, in requests per window.
LIMITS: Dict[str, Tuple[int, int]] = {
    LLM: (_env_int("RATELIMIT_LLM_PER_HOUR", 10), 3600),
    WRITE: (_env_int("RATELIMIT_WRITE_PER_HOUR", 60), 3600),
    READ: (_env_int("RATELIMIT_READ_PER_MIN", 120), 60),
}

# ── The circuit breaker ────────────────────────────────────────────────────
# The budget is denominated in TOKENS, because that is the unit Groq actually
# meters. Capping the number of calls instead looks equivalent and is not:
# measured orchestrator runs range from 8,140 to 18,774 tokens, so any single
# call-count cap is either far too loose for the expensive runs or needlessly
# strict for the cheap ones. Twelve runs might spend 98k tokens or 225k.
#
# Default is 150,000 of the free tier's 200,000 per day, leaving headroom to
# run the eval suite (which costs 66k-98k) after a busy day on the demo.
GLOBAL_LLM_TOKENS_PER_DAY = _env_int("RATELIMIT_GLOBAL_LLM_TOKENS_PER_DAY", 150_000)

# A cheap secondary guard, checked before any tokens are spent: token spend is
# only known *after* a call returns, so without this a burst could all be
# admitted before the first result comes back and updates the total.
GLOBAL_LLM_PER_DAY = _env_int("RATELIMIT_GLOBAL_LLM_PER_DAY", 40)

# A distributed scrape would otherwise grow the tracking table without bound.
MAX_TRACKED_CLIENTS = _env_int("RATELIMIT_MAX_CLIENTS", 10_000)

# Paths that are never limited: liveness must answer while under load, and
# static assets are served straight from disk.
EXEMPT_PREFIXES = ("/health", "/static", "/docs", "/openapi.json", "/redoc")
EXEMPT_PATHS = ("/", "/dashboard")

# Endpoints that reach the model. These are the expensive ones.
_LLM_MARKERS = ("/command", "/assess", "/gaps", "/report", "/demo/simulate")

_lock = threading.Lock()
_hits: Dict[Tuple[str, str], Deque[float]] = {}
_llm_day: Deque[float] = deque()
# (timestamp, tokens) pairs for the rolling 24h window.
_token_day: Deque[Tuple[float, int]] = deque()


def enabled() -> bool:
    return os.environ.get("RELIEFAI_RATELIMIT", "on").lower() not in ("off", "0", "false")


def reset() -> None:
    """Drop all counters. Used by tests."""
    with _lock:
        _hits.clear()
        _llm_day.clear()
        _token_day.clear()


def classify(method: str, path: str) -> str | None:
    """Which tier a request belongs to, or None if it is exempt."""
    if path in EXEMPT_PATHS or path.startswith(EXEMPT_PREFIXES):
        return None
    if any(marker in path for marker in _LLM_MARKERS):
        return LLM
    if method.upper() in ("POST", "PUT", "PATCH", "DELETE"):
        return WRITE
    return READ


def client_ip(headers, fallback: str) -> str:
    """
    The connecting address, given a single trusted proxy in front.

    Render appends the real peer to X-Forwarded-For, so the LAST entry is the
    one it observed. Reading the first entry instead would let a caller pick
    their own identity by sending the header themselves, and with it an
    unlimited supply of fresh rate-limit buckets.
    """
    forwarded = headers.get("x-forwarded-for")
    if forwarded:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return fallback


def _prune(bucket: Deque[float], window: float, now: float) -> None:
    while bucket and now - bucket[0] >= window:
        bucket.popleft()


def _prune_tokens(now: float) -> None:
    while _token_day and now - _token_day[0][0] >= 86400:
        _token_day.popleft()


def record_tokens(count: int) -> None:
    """
    Report tokens actually consumed by a model call.

    Called from the Groq client, so every call is counted — the orchestrator
    loop, the specialist agents and the eval judge alike — rather than only the
    requests that happen to arrive through the HTTP API.
    """
    if count <= 0:
        return
    now = time.time()
    with _lock:
        _prune_tokens(now)
        _token_day.append((now, int(count)))


def tokens_used_today() -> int:
    now = time.time()
    with _lock:
        _prune_tokens(now)
        return sum(n for _, n in _token_day)


def check(ip: str, tier: str) -> Tuple[bool, int]:
    """
    Record a request and say whether it is allowed.

    Returns ``(allowed, retry_after_seconds)``.
    """
    limit, window = LIMITS[tier]
    now = time.time()

    with _lock:
        # The circuit breaker is checked first: it is the failure that actually
        # takes the demo down, and it applies no matter whose address this is.
        if tier == LLM:
            _prune_tokens(now)
            spent = sum(n for _, n in _token_day)
            if GLOBAL_LLM_TOKENS_PER_DAY and spent >= GLOBAL_LLM_TOKENS_PER_DAY:
                oldest = _token_day[0][0] if _token_day else now
                return False, int(86400 - (now - oldest)) + 1

            _prune(_llm_day, 86400, now)
            if GLOBAL_LLM_PER_DAY and len(_llm_day) >= GLOBAL_LLM_PER_DAY:
                return False, int(86400 - (now - _llm_day[0])) + 1

        if limit == 0:
            return True, 0

        key = (ip, tier)
        bucket = _hits.get(key)
        if bucket is None:
            if len(_hits) >= MAX_TRACKED_CLIENTS:
                # Evict whichever bucket has gone longest without a request,
                # so a spray of one-off addresses cannot grow this unbounded.
                oldest = min(_hits, key=lambda k: _hits[k][-1] if _hits[k] else 0)
                _hits.pop(oldest, None)
            bucket = _hits[key] = deque()

        _prune(bucket, window, now)

        if len(bucket) >= limit:
            return False, int(window - (now - bucket[0])) + 1

        bucket.append(now)
        if tier == LLM:
            _llm_day.append(now)
        return True, 0


def snapshot() -> Dict[str, object]:
    """Current counters, for the health endpoint."""
    now = time.time()
    with _lock:
        _prune(_llm_day, 86400, now)
        _prune_tokens(now)
        spent = sum(n for _, n in _token_day)
        return {
            "enabled": enabled(),
            "llm_tokens_today": spent,
            "llm_token_cap": GLOBAL_LLM_TOKENS_PER_DAY,
            "llm_calls_today": len(_llm_day),
            "llm_call_cap": GLOBAL_LLM_PER_DAY,
            "tracked_clients": len(_hits),
        }
