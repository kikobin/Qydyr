"""Air quality that changes decisions. In winter Astana heats with coal, and on calm frosty days
PM2.5 climbs: a long walk is then a bad idea even if it is not that cold.

Data: Open-Meteo Air Quality API (CAMS models), free for non-commercial use, no key.
Attribution: "Air quality: Open-Meteo, CAMS (Copernicus Atmosphere Monitoring Service)".
European AQI bands (EEA 2024): 0–20 good, 20–40 fair, 40–60 moderate, 60–80 poor, 80–100 very poor, >100.
"""
from __future__ import annotations

import json
import logging
import time as _time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

from .. import settings

log = logging.getLogger(__name__)

AIR_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
SOURCE = "Open-Meteo, CAMS"
BANDS = [(20, "хороший"), (40, "приемлемый"), (60, "умеренный"), (80, "плохой"), (100, "очень плохой"),
         (float("inf"), "крайне плохой")]


@dataclass
class Air:
    at: datetime
    pm2_5: float | None
    pm10: float | None
    european_aqi: float | None
    source: str = SOURCE

    @property
    def band(self) -> str:
        if self.european_aqi is None:
            return "нет данных"
        return next(name for hi, name in BANDS if self.european_aqi <= hi)

    @property
    def penalty(self) -> float:
        """Added to the outdoor penalty: clean air changes nothing, smog keeps people inside."""
        a = self.european_aqi
        if a is None or a < 40:
            return 0.0
        return 0.08 if a < 60 else 0.2 if a < 80 else 0.35 if a < 100 else 0.5

    def warning(self) -> str | None:
        a = self.european_aqi
        if a is None or a < 40:
            return None
        pm = f"PM2.5 {self.pm2_5:.0f} мкг/м³" if self.pm2_5 is not None else f"индекс {a:.0f}"
        if a < 60:
            return f"воздух умеренный ({pm}) — долгая прогулка не лучшая идея для чувствительных"
        return f"воздух {self.band} ({pm}) — лучше в помещении"


def _local(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=settings.TZ)


def parse(data: dict, at: datetime) -> Air:
    """Open-Meteo air-quality JSON (timezone=Asia/Almaty) -> Air at `at`."""
    c = data.get("current") or {}
    if c and abs((_local(c["time"]) - at).total_seconds()) < 3600:
        return Air(at=at, pm2_5=c.get("pm2_5"), pm10=c.get("pm10"), european_aqi=c.get("european_aqi"))
    h = data.get("hourly") or {}
    times = [_local(t) for t in h.get("time", [])]
    if not times:
        raise ValueError("no air quality data")
    i = max((k for k, t in enumerate(times) if t <= at), default=0)

    def pick(name: str) -> float | None:
        vals = h.get(name) or []
        return vals[i] if i < len(vals) else None

    return Air(at=at, pm2_5=pick("pm2_5"), pm10=pick("pm10"), european_aqi=pick("european_aqi"))


_CACHE: dict[tuple, tuple[float, dict]] = {}


def fetch(lat: float, lon: float, ttl_s: int = 1800) -> dict:
    key = (round(lat, 1), round(lon, 1))  # the model grid is ~11–45 km: exact coordinates are useless here
    if key in _CACHE and _time.time() - _CACHE[key][0] < ttl_s:
        return _CACHE[key][1]
    params = {"latitude": key[0], "longitude": key[1], "timezone": settings.TZ_NAME, "forecast_days": 2,
              "current": "pm2_5,pm10,european_aqi", "hourly": "pm2_5,pm10,european_aqi"}
    r = httpx.get(AIR_URL, params=params, timeout=10, headers={"User-Agent": settings.USER_AGENT})
    r.raise_for_status()
    data = r.json()
    _CACHE[key] = (_time.time(), data)
    return data


def get(lat: float, lon: float, at: datetime, from_file: Path | None = None) -> Air | None:
    try:
        data = json.loads(from_file.read_text(encoding="utf-8")) if from_file else fetch(lat, lon)
        return parse(data, at)
    except Exception as e:
        log.warning("air quality unavailable: %s", e)
        return None
