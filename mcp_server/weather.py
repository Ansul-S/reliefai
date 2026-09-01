"""
Weather forecasts from Open-Meteo.

This is the only tool that reaches outside ReliefAI's own database, which makes
it the only one that can fail for reasons the operation cannot control. It is
therefore written to *degrade* rather than raise: every failure path returns a
structured, honest answer ("no forecast available, here is why") so the
orchestrator can carry on and say so, instead of the whole run dying on a
network blip.

Three behaviours matter:

  Caching       Geocodes are cached for a day (places do not move) and
                forecasts for fifteen minutes. A dashboard that polls every 30s
                must not become 2,880 requests a day against a free service.

  Stale-on-error  If a refresh fails but an expired entry is still in hand, the
                stale entry is served and flagged. A forecast from 40 minutes
                ago beats no forecast at all.

  Offline mode  RELIEFAI_WEATHER_MODE=offline returns a fixed payload without
                touching the network, so evals and tests stay deterministic.

Open-Meteo needs no API key and no registration.
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional, Tuple

import httpx

_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Short: the agent is inside a tool-calling loop and a hung upstream would stall
# the whole run. Better a fast honest "unavailable" than a slow maybe.
_TIMEOUT_S = 6.0

_GEOCODE_TTL_S = 24 * 60 * 60
_FORECAST_TTL_S = 15 * 60

# Administrative prefixes the Open-Meteo geocoder does not match. It resolves
# bare place names only: "Metro Manila" returns nothing, "Manila" resolves.
_ADMIN_PREFIXES = (
    "metro ", "metropolitan ", "greater ", "city of ", "province of ",
    "district of ", "region of ", "municipality of ", "state of ",
)

_geocode_cache: Dict[str, Tuple[float, Optional[Dict[str, Any]]]] = {}
_forecast_cache: Dict[Tuple[float, float], Tuple[float, Dict[str, Any]]] = {}


def _offline() -> bool:
    return os.environ.get("RELIEFAI_WEATHER_MODE", "").lower() == "offline"


def clear_cache() -> None:
    """Drop every cached entry. Used by tests."""
    _geocode_cache.clear()
    _forecast_cache.clear()


def _unavailable(reason: str, location: str) -> Dict[str, Any]:
    """
    The single shape every failure returns.

    ``available`` is explicitly False rather than the key being absent, so a
    model reading this cannot mistake a failed lookup for a calm forecast.
    """
    return {
        "available": False,
        "location": location,
        "reason": reason,
        "guidance": (
            "No forecast could be retrieved. Say so plainly rather than "
            "guessing at conditions, and continue with the rest of the task."
        ),
    }


def _candidate_names(location: str):
    """
    Names to try, most specific first.

    "Metro Manila, Philippines" yields: the whole string, then "Metro Manila",
    then "Manila" (prefix stripped), then "Philippines" as a coarse fallback.
    """
    seen = set()
    raw = (location or "").strip()
    if not raw:
        return

    parts = [p.strip() for p in raw.split(",") if p.strip()]

    def _emit(name: str, approximate: bool):
        key = name.lower()
        if key and key not in seen:
            seen.add(key)
            return (name, approximate)
        return None

    ordered = []
    for candidate in (raw, parts[0] if parts else ""):
        got = _emit(candidate, False)
        if got:
            ordered.append(got)

    # Strip a leading administrative word: "Metro Manila" -> "Manila".
    if parts:
        head = parts[0]
        lowered = head.lower()
        for prefix in _ADMIN_PREFIXES:
            if lowered.startswith(prefix):
                got = _emit(head[len(prefix):].strip(), False)
                if got:
                    ordered.append(got)
                break

    # Last resort: the trailing segment, usually a country. A country centroid
    # is a poor forecast for a city, so it is flagged as approximate.
    if len(parts) > 1:
        got = _emit(parts[-1], True)
        if got:
            ordered.append(got)

    for item in ordered:
        yield item


async def _geocode(location: str) -> Optional[Dict[str, Any]]:
    """Resolve a location string to coordinates, or None."""
    now = time.time()
    cached = _geocode_cache.get(location)
    if cached and now - cached[0] < _GEOCODE_TTL_S:
        return cached[1]

    resolved: Optional[Dict[str, Any]] = None
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            for name, approximate in _candidate_names(location):
                response = await client.get(
                    _GEOCODE_URL, params={"name": name, "count": 1}
                )
                response.raise_for_status()
                results = (response.json() or {}).get("results") or []
                if results:
                    hit = results[0]
                    resolved = {
                        "latitude": hit["latitude"],
                        "longitude": hit["longitude"],
                        "resolved_name": ", ".join(
                            p for p in (hit.get("name"), hit.get("country")) if p
                        ),
                        "approximate": approximate,
                    }
                    break
    except Exception:
        # A geocode failure is not fatal: if a previous answer is cached, even
        # an expired one, it is still the right coordinates.
        if cached:
            return cached[1]
        return None

    _geocode_cache[location] = (now, resolved)
    return resolved


def _summarise(hourly: Dict[str, Any], units: Dict[str, Any]) -> Dict[str, Any]:
    """
    Condense the raw hourly arrays into what a coordinator would actually ask.

    The agent is given a summary plus a short hourly window rather than 48
    rows: a wall of numbers invites the model to quote one at random.
    """
    times = hourly.get("time", [])[:24]
    precip = [p or 0 for p in hourly.get("precipitation", [])[:24]]
    wind = [w or 0 for w in hourly.get("wind_speed_10m", [])[:24]]

    if not times:
        return {}

    total_precip = round(sum(precip), 1)
    peak_wind = round(max(wind), 1) if wind else 0.0
    first_rain = next(
        (times[i] for i, p in enumerate(precip) if p >= 0.2), None
    )

    return {
        "hours_covered": len(times),
        "total_precipitation": total_precip,
        "precipitation_unit": units.get("precipitation", "mm"),
        "peak_wind_speed": peak_wind,
        "wind_speed_unit": units.get("wind_speed_10m", "km/h"),
        "rain_starts_at": first_rain,
        "next_6_hours": [
            {"time": times[i], "precipitation": precip[i], "wind_speed": wind[i]}
            for i in range(min(6, len(times)))
        ],
    }


_OFFLINE_PAYLOAD = {
    "available": True,
    "offline_fixture": True,
    "location": "offline fixture",
    "resolved_name": "Test Location",
    "latitude": 0.0,
    "longitude": 0.0,
    "timezone": "UTC",
    "summary": {
        "hours_covered": 24,
        "total_precipitation": 0.0,
        "precipitation_unit": "mm",
        "peak_wind_speed": 0.0,
        "wind_speed_unit": "km/h",
        "rain_starts_at": None,
        "next_6_hours": [],
    },
}


async def get_forecast(location: str) -> Dict[str, Any]:
    """
    Fetch a 24-hour forecast for a location string. Never raises.

    Returns either ``{"available": True, ...}`` with a summary, or the
    ``available: False`` shape explaining what went wrong.
    """
    if _offline():
        return {**_OFFLINE_PAYLOAD, "location": location}

    if not (location or "").strip():
        return _unavailable("No location was supplied.", location or "")

    place = await _geocode(location)
    if not place:
        return _unavailable(
            f"Could not resolve {location!r} to coordinates.", location
        )

    key = (place["latitude"], place["longitude"])
    now = time.time()
    cached = _forecast_cache.get(key)
    if cached and now - cached[0] < _FORECAST_TTL_S:
        return {**cached[1], "location": location, "cached": True}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            response = await client.get(
                _FORECAST_URL,
                params={
                    "latitude": place["latitude"],
                    "longitude": place["longitude"],
                    "hourly": "precipitation,wind_speed_10m",
                    "forecast_days": 2,
                    "timezone": "auto",
                },
            )
            response.raise_for_status()
            payload = response.json() or {}
    except Exception as exc:
        # Stale beats nothing: a 40-minute-old forecast still tells a
        # coordinator whether rain is coming.
        if cached:
            return {
                **cached[1],
                "location": location,
                "cached": True,
                "stale": True,
                "note": (
                    "Upstream forecast refresh failed; serving the most recent "
                    "successful reading."
                ),
            }
        return _unavailable(
            f"Forecast service unreachable ({type(exc).__name__}).", location
        )

    summary = _summarise(payload.get("hourly", {}), payload.get("hourly_units", {}))
    if not summary:
        return _unavailable("Forecast service returned no hourly data.", location)

    result = {
        "available": True,
        "location": location,
        "resolved_name": place["resolved_name"],
        "latitude": place["latitude"],
        "longitude": place["longitude"],
        "timezone": payload.get("timezone"),
        "summary": summary,
        "source": "Open-Meteo",
    }
    if place.get("approximate"):
        result["precision_warning"] = (
            f"{location!r} matched only at country level; coordinates are "
            "approximate and the forecast may not reflect local conditions."
        )

    _forecast_cache[key] = (now, result)
    return result
