"""Honest travel times: street distance (not a straight line) and a walking speed that depends on
who is walking and the weather. Plug a real router with QYDYR_OSRM_URL for exact street routes."""
from __future__ import annotations

import logging
import math
import os

import httpx

from .weather import Weather

log = logging.getLogger(__name__)

DETOUR = 1.3            # street network vs straight line, typical for a grid city
WALK_KMH = 4.8
KIDS_KMH = 3.0
WHEELCHAIR_KMH = 3.5
TAXI_KMH = 25.0         # average city speed incl. traffic lights
TAXI_PICKUP_MIN = 5
OSRM_URL = os.getenv("QYDYR_OSRM_URL")  # e.g. http://localhost:5000 with the foot profile


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def walk_speed_kmh(*, kids: bool = False, wheelchair: bool = False, weather: Weather | None = None) -> float:
    v = KIDS_KMH if kids else WHEELCHAIR_KMH if wheelchair else WALK_KMH
    if weather is not None and (weather.snow_cm > 0 or weather.feels_c <= -15):
        v *= 0.85  # snow, ice, bundled up
    return v


def street_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    return haversine_m(lat1, lon1, lat2, lon2) * DETOUR


def walk_min(lat1: float, lon1: float, lat2: float, lon2: float, *, kids: bool = False, wheelchair: bool = False,
             weather: Weather | None = None) -> int:
    d = street_m(lat1, lon1, lat2, lon2)
    if d < 60:
        return 0  # same building / next door
    return max(1, round(d / 1000 / walk_speed_kmh(kids=kids, wheelchair=wheelchair, weather=weather) * 60))


def taxi_min(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    """City streets ~25 km/h; longer trips go over avenues and the airport road (~38 km/h)."""
    km = haversine_m(lat1, lon1, lat2, lon2) * 1.35 / 1000
    kmh = TAXI_KMH if km < 3 else 38.0
    return round(km / kmh * 60) + TAXI_PICKUP_MIN


def osrm_geometry(points: list[list[float]]) -> list[list[float]] | None:
    """Street path through [[lon, lat], ...] from a configured OSRM (foot profile); None without one."""
    if not OSRM_URL or len(points) < 2:
        return None
    try:
        coords = ";".join(f"{lon},{lat}" for lon, lat in points)
        r = httpx.get(f"{OSRM_URL}/route/v1/foot/{coords}", params={"overview": "full", "geometries": "geojson"},
                      timeout=4)
        r.raise_for_status()
        return r.json()["routes"][0]["geometry"]["coordinates"]
    except Exception as e:
        log.debug("OSRM unavailable: %s", e)
        return None


def osrm_walk_min(lat1: float, lon1: float, lat2: float, lon2: float) -> int | None:
    """Exact street route if a router is configured; None otherwise (callers fall back to walk_min)."""
    if not OSRM_URL:
        return None
    try:
        r = httpx.get(f"{OSRM_URL}/route/v1/foot/{lon1},{lat1};{lon2},{lat2}", params={"overview": "false"}, timeout=3)
        r.raise_for_status()
        return round(r.json()["routes"][0]["duration"] / 60)
    except Exception as e:
        log.debug("OSRM unavailable: %s", e)
        return None
