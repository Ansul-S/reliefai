"""
Tests for the weather tool.

This is the only tool that depends on a service outside the project, so what
matters most is not that it works when the network is healthy — it is that it
fails *usefully* when the network is not. Every test here runs offline; none
touches Open-Meteo.
"""
from __future__ import annotations

import httpx
import pytest

from mcp_server import weather


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """
    Importing ``evals`` sets RELIEFAI_WEATHER_MODE=offline process-wide, and
    pytest collects a module that imports it. Without clearing the variable
    here, every degradation test below would silently pass by short-circuiting
    into the offline fixture instead of exercising the failure path.
    """
    monkeypatch.delenv("RELIEFAI_WEATHER_MODE", raising=False)
    weather.clear_cache()
    yield
    weather.clear_cache()


# ---------------------------------------------------------------------------
# The location ladder
# ---------------------------------------------------------------------------


def test_candidate_names_strips_administrative_prefix():
    """
    Open-Meteo's geocoder matches bare place names only: "Metro Manila"
    resolves to nothing while "Manila" resolves. The seeded disaster's location
    is "Metro Manila, Philippines", so without prefix stripping the only
    disaster in the demo database would have no forecast at all.
    """
    names = [n for n, _ in weather._candidate_names("Metro Manila, Philippines")]
    assert "Manila" in names
    assert names.index("Metro Manila, Philippines") < names.index("Manila")


def test_candidate_names_falls_back_to_country_and_flags_it():
    candidates = list(weather._candidate_names("Nowhereville, Philippines"))
    country = [(n, approx) for n, approx in candidates if n == "Philippines"]
    assert country, "the trailing segment should be tried as a last resort"
    assert country[0][1] is True, "a country centroid must be flagged approximate"


def test_candidate_names_does_not_repeat_itself():
    names = [n for n, _ in weather._candidate_names("Manila")]
    assert len(names) == len(set(names))


def test_candidate_names_handles_empty_input():
    assert list(weather._candidate_names("")) == []
    assert list(weather._candidate_names("   ")) == []


# ---------------------------------------------------------------------------
# Degradation — the point of the exercise
# ---------------------------------------------------------------------------


async def test_forecast_unavailable_when_service_is_down(monkeypatch):
    """A dead upstream must produce an answer, not an exception."""

    class DeadClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(weather.httpx, "AsyncClient", DeadClient)

    result = await weather.get_forecast("Manila")
    assert result["available"] is False
    assert "resolve" in result["reason"].lower() or "unreachable" in result["reason"].lower()
    assert "guidance" in result


async def test_stale_forecast_is_served_when_refresh_fails(monkeypatch):
    """
    A forecast from earlier still tells a coordinator whether rain is coming,
    so an expired entry beats no answer when the refresh fails.
    """
    weather._geocode_cache["Manila"] = (
        weather.time.time(),
        {"latitude": 14.6, "longitude": 121.0,
         "resolved_name": "Manila, Philippines", "approximate": False},
    )
    # An entry that is present but past its TTL.
    weather._forecast_cache[(14.6, 121.0)] = (
        weather.time.time() - (weather._FORECAST_TTL_S + 60),
        {"available": True, "resolved_name": "Manila, Philippines",
         "summary": {"total_precipitation": 4.2}},
    )

    class DeadClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k):
            raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(weather.httpx, "AsyncClient", DeadClient)

    result = await weather.get_forecast("Manila")
    assert result["available"] is True
    assert result["stale"] is True
    assert result["summary"]["total_precipitation"] == 4.2
    assert "note" in result


async def test_unavailable_shape_states_availability_explicitly():
    """
    ``available`` is False rather than absent so a model reading the payload
    cannot mistake a failed lookup for a calm forecast.
    """
    result = await weather.get_forecast("")
    assert result["available"] is False
    assert "available" in result


async def test_offline_mode_never_touches_the_network(monkeypatch):
    """Evals must stay deterministic, so offline mode short-circuits first."""

    class Exploding:
        def __init__(self, *a, **k):
            raise AssertionError("offline mode must not open a client")

    monkeypatch.setattr(weather.httpx, "AsyncClient", Exploding)
    monkeypatch.setenv("RELIEFAI_WEATHER_MODE", "offline")

    result = await weather.get_forecast("Metro Manila, Philippines")
    assert result["available"] is True
    assert result["offline_fixture"] is True


# ---------------------------------------------------------------------------
# Summarising
# ---------------------------------------------------------------------------


def test_summary_reports_when_rain_starts():
    hourly = {
        "time": ["T0", "T1", "T2", "T3"],
        "precipitation": [0.0, 0.0, 0.9, 1.1],
        "wind_speed_10m": [10.0, 12.0, 30.5, 21.0],
    }
    s = weather._summarise(hourly, {"precipitation": "mm", "wind_speed_10m": "km/h"})
    assert s["rain_starts_at"] == "T2"
    assert s["total_precipitation"] == 2.0
    assert s["peak_wind_speed"] == 30.5


def test_summary_reports_no_rain_as_none_not_zero():
    """A dry forecast must be distinguishable from 'it rains at hour zero'."""
    hourly = {
        "time": ["T0", "T1"],
        "precipitation": [0.0, 0.1],
        "wind_speed_10m": [5.0, 6.0],
    }
    s = weather._summarise(hourly, {})
    assert s["rain_starts_at"] is None


def test_summary_survives_empty_payload():
    assert weather._summarise({}, {}) == {}


# ---------------------------------------------------------------------------
# The forecast window starts now, not at midnight
# ---------------------------------------------------------------------------

from datetime import datetime, timedelta, timezone  # noqa: E402

MANILA_OFFSET = 8 * 3600
# 15:52 in Manila — the moment the defect was observed against the live API.
MANILA_AFTERNOON = datetime(2026, 10, 20, 7, 52, tzinfo=timezone.utc)


def _two_days_from_local_midnight(rain_hours=()):
    """Open-Meteo's shape: 48 local hourly stamps starting at 00:00 today."""
    start = datetime(2026, 10, 20, 0, 0)
    times = [(start + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(48)]
    return {
        "time": times,
        "precipitation": [2.0 if h in rain_hours else 0.0 for h in range(48)],
        "wind_speed_10m": [10.0] * 48,
    }


def test_window_starts_at_the_current_local_hour():
    """
    Regression: the first 24 entries were read as "the next 24 hours", but
    the series starts at local midnight. At 15:52 the old window was 16 hours
    of history, and "next_6_hours" was 00:00-05:00.
    """
    s = weather._summarise(
        _two_days_from_local_midnight(), {},
        utc_offset_seconds=MANILA_OFFSET, now=MANILA_AFTERNOON,
    )
    assert s["next_6_hours"][0]["time"] == "2026-10-20T15:00"
    assert s["hours_covered"] == 24


def test_rain_that_already_fell_is_not_forecast():
    """Rain at 03:00 this morning is not rain coming in the next 24 hours."""
    s = weather._summarise(
        _two_days_from_local_midnight(rain_hours={3}), {},
        utc_offset_seconds=MANILA_OFFSET, now=MANILA_AFTERNOON,
    )
    assert s["total_precipitation"] == 0.0
    assert s["rain_starts_at"] is None


def test_rain_tonight_is_forecast():
    s = weather._summarise(
        _two_days_from_local_midnight(rain_hours={21}), {},
        utc_offset_seconds=MANILA_OFFSET, now=MANILA_AFTERNOON,
    )
    assert s["rain_starts_at"] == "2026-10-20T21:00"
    assert s["total_precipitation"] == 2.0


def test_a_series_entirely_in_the_past_is_no_forecast():
    a_week_later = MANILA_AFTERNOON + timedelta(days=7)
    assert weather._summarise(
        _two_days_from_local_midnight(), {},
        utc_offset_seconds=MANILA_OFFSET, now=a_week_later,
    ) == {}


async def test_get_forecast_reads_the_window_from_now(monkeypatch):
    """End to end through get_forecast, with the clock fixed."""
    forecast = {
        "timezone": "Asia/Manila",
        "utc_offset_seconds": MANILA_OFFSET,
        "hourly": _two_days_from_local_midnight(rain_hours={2, 18}),
        "hourly_units": {"precipitation": "mm", "wind_speed_10m": "km/h"},
    }

    class Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None):
            body = (
                {"results": [{"latitude": 14.6, "longitude": 121.0,
                              "name": "Manila", "country": "Philippines"}]}
                if "geocoding" in url else forecast
            )
            return httpx.Response(200, json=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(weather.httpx, "AsyncClient", Client)
    monkeypatch.setattr(weather, "_now_utc", lambda: MANILA_AFTERNOON)

    result = await weather.get_forecast("Manila")
    assert result["available"] is True
    assert result["summary"]["rain_starts_at"] == "2026-10-20T18:00"
    assert result["summary"]["total_precipitation"] == 2.0
