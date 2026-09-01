"""
Tests for rate limiting and the spend circuit breaker.

The interesting cases are not "does it count to ten". They are the two ways a
limiter is commonly wrong: trusting a header a caller controls, so anyone can
mint fresh buckets; and limiting per address only, so a distributed bot walks
straight past it and drains the model budget anyway.
"""
from __future__ import annotations

import pytest

import ratelimit


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.delenv("RELIEFAI_RATELIMIT", raising=False)
    ratelimit.reset()
    yield
    ratelimit.reset()


# ---------------------------------------------------------------------------
# Classification — tiers follow cost, matching how auth is gated
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method,path,expected", [
    ("POST", "/disasters/1/command", ratelimit.LLM),
    ("POST", "/disasters/1/supplies/assess", ratelimit.LLM),
    ("POST", "/disasters/1/shifts/gaps", ratelimit.LLM),
    ("GET", "/disasters/1/report", ratelimit.LLM),
    ("POST", "/disasters/1/tasks", ratelimit.WRITE),
    ("PATCH", "/tasks/1", ratelimit.WRITE),
    ("GET", "/disasters", ratelimit.READ),
    ("GET", "/disasters/1/supplies", ratelimit.READ),
])
def test_requests_are_tiered_by_what_they_cost(method, path, expected):
    assert ratelimit.classify(method, path) == expected


def test_report_is_treated_as_expensive_despite_being_a_get():
    """GET /report runs the report agent, so verb alone must not decide."""
    assert ratelimit.classify("GET", "/disasters/1/report") == ratelimit.LLM


@pytest.mark.parametrize("path", ["/health", "/", "/dashboard", "/static/css/app.css", "/docs"])
def test_liveness_and_assets_are_never_limited(path):
    assert ratelimit.classify("GET", path) is None


# ---------------------------------------------------------------------------
# Client identity — the header is attacker-controlled
# ---------------------------------------------------------------------------


def test_client_ip_uses_the_last_forwarded_entry():
    """
    A caller can send their own X-Forwarded-For; the proxy appends the address
    it actually saw. Reading the first entry would let anyone mint a fresh
    bucket per request by varying a header they control.
    """
    headers = {"x-forwarded-for": "1.2.3.4, 5.6.7.8, 203.0.113.9"}
    assert ratelimit.client_ip(headers, "fallback") == "203.0.113.9"


def test_client_ip_falls_back_when_header_absent():
    assert ratelimit.client_ip({}, "198.51.100.7") == "198.51.100.7"


def test_spoofed_forwarded_header_cannot_mint_new_buckets(monkeypatch):
    monkeypatch.setitem(ratelimit.LIMITS, ratelimit.WRITE, (2, 3600))
    real = "203.0.113.9"
    for spoof in ("a", "b", "c"):
        ip = ratelimit.client_ip({"x-forwarded-for": f"{spoof}, {real}"}, "fallback")
        assert ip == real
    assert ratelimit.check(real, ratelimit.WRITE)[0] is True
    assert ratelimit.check(real, ratelimit.WRITE)[0] is True
    assert ratelimit.check(real, ratelimit.WRITE)[0] is False


# ---------------------------------------------------------------------------
# Per-IP limiting
# ---------------------------------------------------------------------------


def test_requests_are_allowed_up_to_the_limit_then_refused(monkeypatch):
    monkeypatch.setitem(ratelimit.LIMITS, ratelimit.READ, (3, 60))
    for _ in range(3):
        assert ratelimit.check("10.0.0.1", ratelimit.READ)[0] is True
    allowed, retry = ratelimit.check("10.0.0.1", ratelimit.READ)
    assert allowed is False
    assert 0 < retry <= 61


def test_one_client_being_limited_does_not_affect_another(monkeypatch):
    monkeypatch.setitem(ratelimit.LIMITS, ratelimit.READ, (1, 60))
    assert ratelimit.check("10.0.0.1", ratelimit.READ)[0] is True
    assert ratelimit.check("10.0.0.1", ratelimit.READ)[0] is False
    assert ratelimit.check("10.0.0.2", ratelimit.READ)[0] is True


def test_window_slides(monkeypatch):
    monkeypatch.setitem(ratelimit.LIMITS, ratelimit.READ, (1, 60))
    assert ratelimit.check("10.0.0.3", ratelimit.READ)[0] is True
    assert ratelimit.check("10.0.0.3", ratelimit.READ)[0] is False

    # Age the recorded hit past the window rather than sleeping through it.
    bucket = ratelimit._hits[("10.0.0.3", ratelimit.READ)]
    bucket[0] -= 61
    assert ratelimit.check("10.0.0.3", ratelimit.READ)[0] is True


# ---------------------------------------------------------------------------
# The circuit breaker — the layer that survives a distributed bot
# ---------------------------------------------------------------------------


def test_global_cap_stops_a_distributed_bot(monkeypatch):
    """
    Per-IP limits are useless against many addresses making one call each.
    The daily cap is what actually protects the token budget.
    """
    monkeypatch.setattr(ratelimit, "GLOBAL_LLM_PER_DAY", 5)
    monkeypatch.setattr(ratelimit, "GLOBAL_LLM_TOKENS_PER_DAY", 10_000_000)
    monkeypatch.setitem(ratelimit.LIMITS, ratelimit.LLM, (100, 3600))

    for i in range(5):
        assert ratelimit.check(f"10.0.{i}.1", ratelimit.LLM)[0] is True, i

    # A sixth, from an address that has never been seen before.
    allowed, retry = ratelimit.check("10.0.99.1", ratelimit.LLM)
    assert allowed is False
    assert retry > 0


def test_breaker_counts_tokens_not_calls(monkeypatch):
    """
    Measured orchestrator runs span 8,140 to 18,774 tokens, so a call-count cap
    is either far too loose for expensive runs or needlessly strict for cheap
    ones. The budget Groq meters is tokens, so that is what is capped.
    """
    monkeypatch.setattr(ratelimit, "GLOBAL_LLM_TOKENS_PER_DAY", 20_000)
    monkeypatch.setattr(ratelimit, "GLOBAL_LLM_PER_DAY", 0)
    monkeypatch.setitem(ratelimit.LIMITS, ratelimit.LLM, (100, 3600))

    assert ratelimit.check("10.0.0.1", ratelimit.LLM)[0] is True
    ratelimit.record_tokens(18_774)
    assert ratelimit.check("10.0.0.1", ratelimit.LLM)[0] is True
    ratelimit.record_tokens(8_140)

    # 26,914 spent against a 20,000 cap — closed to everyone, new IPs included.
    assert ratelimit.check("10.0.0.99", ratelimit.LLM)[0] is False
    assert ratelimit.tokens_used_today() == 26_914


def test_call_cap_guards_the_window_before_any_tokens_are_known(monkeypatch):
    """
    Spend is only known after a call returns, so a simultaneous burst could all
    be admitted before the first result lands. The call cap covers that gap.
    """
    monkeypatch.setattr(ratelimit, "GLOBAL_LLM_TOKENS_PER_DAY", 10_000_000)
    monkeypatch.setattr(ratelimit, "GLOBAL_LLM_PER_DAY", 3)
    monkeypatch.setitem(ratelimit.LIMITS, ratelimit.LLM, (100, 3600))

    for i in range(3):
        assert ratelimit.check(f"10.0.{i}.1", ratelimit.LLM)[0] is True
    assert ratelimit.check("10.0.9.1", ratelimit.LLM)[0] is False


def test_record_tokens_ignores_nonpositive():
    ratelimit.record_tokens(0)
    ratelimit.record_tokens(-50)
    assert ratelimit.tokens_used_today() == 0


def test_global_cap_does_not_restrict_reads(monkeypatch):
    """Exhausting the model budget must not take the dashboard down with it."""
    monkeypatch.setattr(ratelimit, "GLOBAL_LLM_PER_DAY", 1)
    monkeypatch.setattr(ratelimit, "GLOBAL_LLM_TOKENS_PER_DAY", 10_000_000)
    assert ratelimit.check("10.0.0.1", ratelimit.LLM)[0] is True
    assert ratelimit.check("10.0.0.2", ratelimit.LLM)[0] is False
    assert ratelimit.check("10.0.0.3", ratelimit.READ)[0] is True


# ---------------------------------------------------------------------------
# Memory safety
# ---------------------------------------------------------------------------


def test_tracking_table_is_bounded(monkeypatch):
    """A spray of one-off addresses must not grow the table without limit."""
    monkeypatch.setattr(ratelimit, "MAX_TRACKED_CLIENTS", 25)
    for i in range(300):
        ratelimit.check(f"10.{i // 256}.{i % 256}.5", ratelimit.READ)
    assert len(ratelimit._hits) <= 25


def test_snapshot_reports_breaker_state(monkeypatch):
    monkeypatch.setattr(ratelimit, "GLOBAL_LLM_PER_DAY", 7)
    ratelimit.check("10.0.0.1", ratelimit.LLM)
    ratelimit.record_tokens(4_200)
    snap = ratelimit.snapshot()
    assert snap["llm_calls_today"] == 1
    assert snap["llm_call_cap"] == 7
    assert snap["llm_tokens_today"] == 4_200


def test_limiter_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("RELIEFAI_RATELIMIT", "off")
    assert ratelimit.enabled() is False
