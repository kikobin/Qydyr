"""Weather that changes decisions: feels-like temperature, wind, snow in the next hours, sunset.

Astana is one of the coldest capitals in the world, so a 20-minute walk at −25 with wind is not
a "walk". The assistant turns weather into an outdoor penalty, a comfortable outdoor-minutes limit,
and plain warnings ("через 40 минут пойдёт снег").
Data: Open-Meteo (free, no key). Without weather data the assistant still works, just says so.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .. import settings
from .air import FILE_SOURCE, Air, cached_json
from .sun import sun_times

log = logging.getLogger(__name__)

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
SNOW_CODES = {71, 73, 75, 77, 85, 86}
RAIN_CODES = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82}
STORM_CODES = {95, 96, 99}
ASTANA = (51.1282, 71.4304)

# The time & weather machine: "what would you advise in a −25 blizzard?" The answer is recomputed for
# real places and hours, only the weather is a what-if — and the answer says so.
PRESETS: dict[str, dict] = {
    "blizzard": {"label": "метель −25", "temp": -25, "feels": -34, "wind": 13, "precip": 0.6, "snow": 1.2,
                 "code": 75},
    "frost": {"label": "мороз −30, ясно", "temp": -30, "feels": -37, "wind": 4, "precip": 0, "snow": 0, "code": 0},
    "rain": {"label": "дождь +8", "temp": 8, "feels": 5, "wind": 6, "precip": 1.8, "snow": 0, "code": 63},
    "summer_evening": {"label": "летний вечер +24", "temp": 24, "feels": 24, "wind": 3, "precip": 0, "snow": 0,
                       "code": 0, "sunrise": "03:58", "sunset": "20:33"},
    "smog": {"label": "штиль −15 и смог", "temp": -15, "feels": -18, "wind": 1, "precip": 0, "snow": 0, "code": 45,
             "aqi": 92, "pm2_5": 74, "pm10": 96},
}


@dataclass
class Hour:
    at: datetime
    temp_c: float
    feels_c: float
    wind_ms: float
    precip_mm: float
    snow_cm: float
    code: int


@dataclass
class Weather:
    at: datetime
    temp_c: float
    feels_c: float
    wind_ms: float
    precip_mm: float
    snow_cm: float
    code: int
    sunrise: datetime | None = None
    sunset: datetime | None = None
    hours: list[Hour] = field(default_factory=list)
    source: str = "Open-Meteo"
    air: Air | None = None
    simulated: str | None = None      # preset label when this is a what-if

    # --- what it means for a walk ---
    @property
    def level(self) -> str:
        """Дубак-о-метр."""
        f = self.feels_c
        if f >= 18:
            return "тепло"
        if f >= 8:
            return "свежо"
        if f >= 0:
            return "бодрит"
        if f >= -10:
            return "дубак"
        if f >= -20:
            return "дубачина"
        return "Астана classic"

    @property
    def outdoor_penalty(self) -> float:
        """0 = perfect for outdoors, 1 = stay inside."""
        f = self.feels_c
        p = 0.0 if f >= 15 else 0.05 if f >= 8 else 0.15 if f >= 0 else 0.35 if f >= -10 else 0.6 if f >= -20 else 0.85
        if self.wind_ms >= 10:
            p += 0.15
        if self.precip_now:
            p += 0.2
        if f >= 30:
            p += 0.3  # heat
        if self.air is not None:
            p += self.air.penalty
        return min(1.0, p)

    @property
    def max_outdoor_min(self) -> int:
        """Comfortable time outside in one go (a comfort heuristic, not medical advice)."""
        f = self.feels_c
        base = 120 if f >= 0 else 40 if f >= -10 else 20 if f >= -20 else 10
        return base // 2 if self.wind_ms >= 12 else base

    @property
    def precip_now(self) -> bool:
        return self.precip_mm > 0.1 or self.snow_cm > 0 or self.code in SNOW_CODES | RAIN_CODES | STORM_CODES

    def is_dark(self, at: datetime) -> bool:
        if not (self.sunrise and self.sunset):
            return False
        return not (self.sunrise <= at <= self.sunset)

    def golden_hour(self, at: datetime) -> bool:
        """The last ~50 minutes before sunset: the best light for photos of the skyline."""
        return bool(self.sunset and self.sunset - timedelta(minutes=50) <= at <= self.sunset + timedelta(minutes=5))

    def comfort(self, at: datetime) -> tuple[int, str, list[str]]:
        """Comfort of being outside: 4 = perfect … 0 = stay in, with the factors that drive it."""
        p = self.outdoor_penalty
        level = 4 if p < 0.1 else 3 if p < 0.2 else 2 if p < 0.4 else 1 if p < 0.65 else 0
        label = ["лучше в помещении", "только короткими перебежками", "прохладно, оденьтесь теплее",
                 "комфортно", "отлично для прогулки"][level]
        if self.air is not None and self.air.penalty >= 0.2:  # smog beats a nice temperature
            level = min(level, 1 if self.air.penalty < 0.35 else 0)
            label = "на улице недолго: воздух плохой" if level == 1 else "лучше в помещении: воздух очень плохой"
        factors = [f"ощущается {self.feels_c:+.0f}°"]
        if self.wind_ms >= 8:
            factors.append(f"ветер {self.wind_ms:.0f} м/с")
        if self.precip_now:
            factors.append("снег" if self.snow_cm > 0 or self.code in SNOW_CODES else "осадки")
        if self.air is not None and self.air.penalty > 0:
            factors.append(f"воздух {self.air.band}")
        if self.is_dark(at):
            factors.append("темно")
        return level, label, factors

    def warnings(self, at: datetime, horizon_min: int = 180) -> list[str]:
        out: list[str] = []
        if self.air is not None and (aw := self.air.warning()):
            out.append(aw)
        if not self.precip_now:
            for h in self.hours:
                mins = int((h.at - at).total_seconds() // 60)
                if 0 < mins <= horizon_min and (h.snow_cm > 0 or h.code in SNOW_CODES):
                    out.append(f"через {mins} мин ожидается снег")
                    break
                if 0 < mins <= horizon_min and (h.precip_mm > 0.2 or h.code in RAIN_CODES):
                    out.append(f"через {mins} мин ожидается дождь")
                    break
        if self.wind_ms >= 12:
            out.append(f"сильный ветер {self.wind_ms:.0f} м/с")
        if self.sunset and at < self.sunset <= at + timedelta(minutes=horizon_min):
            out.append(f"закат в {self.sunset.astimezone(settings.TZ):%H:%M}")
        return out

    def summary(self) -> str:
        s = f"{self.temp_c:+.0f}°, ощущается {self.feels_c:+.0f}°, ветер {self.wind_ms:.0f} м/с, «{self.level}»"
        if self.precip_now:
            s += ", осадки"
        if self.air is not None and self.air.penalty > 0:
            s += f", воздух {self.air.band}"
        if self.simulated:
            s += f" (симуляция: {self.simulated})"
        return s


def preset(name: str, at: datetime, real: Weather | None = None, lat: float = ASTANA[0],
           lon: float = ASTANA[1]) -> Weather:
    """A what-if weather for the time & weather machine. Raises KeyError for an unknown preset."""
    p = PRESETS[name]
    day = at.astimezone(settings.TZ).date()
    if "sunset" in p:
        rise = datetime.combine(day, datetime.strptime(p["sunrise"], "%H:%M").time(), tzinfo=settings.TZ)
        sset = datetime.combine(day, datetime.strptime(p["sunset"], "%H:%M").time(), tzinfo=settings.TZ)
    elif real is not None and real.sunset:
        rise, sset = real.sunrise, real.sunset
    else:
        rise, sset = sun_times(day, lat, lon, settings.TZ) or (None, None)
    air = Air(at=at, pm2_5=p.get("pm2_5", 6), pm10=p.get("pm10", 12), european_aqi=p.get("aqi", 15),
              source="симуляция")
    return Weather(at=at, temp_c=p["temp"], feels_c=p["feels"], wind_ms=p["wind"], precip_mm=p["precip"],
                   snow_cm=p["snow"], code=p["code"], sunrise=rise, sunset=sset, hours=[], air=air,
                   source=f"симуляция: {p['label']}", simulated=p["label"])


def _local(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=settings.TZ)


def parse(data: dict, at: datetime) -> Weather:
    """Open-Meteo JSON (requested with timezone=Asia/Almaty, wind in m/s) -> Weather at `at`."""
    hourly = data.get("hourly", {})
    hours = [
        Hour(at=_local(t), temp_c=hourly["temperature_2m"][i], feels_c=hourly["apparent_temperature"][i],
             wind_ms=hourly["wind_speed_10m"][i], precip_mm=(hourly.get("precipitation") or [0] * len(hourly["time"]))[i],
             snow_cm=(hourly.get("snowfall") or [0] * len(hourly["time"]))[i],
             code=(hourly.get("weather_code") or [0] * len(hourly["time"]))[i])
        for i, t in enumerate(hourly.get("time", []))
    ]
    # current conditions: the hourly slot containing `at` (works for "what if" times too)
    cur = next((h for h in reversed(hours) if h.at <= at), hours[0] if hours else None)
    c = data.get("current") or {}
    use_current = c and abs((_local(c["time"]) - at).total_seconds()) < 3600
    daily = data.get("daily", {})
    day = at.astimezone(settings.TZ).date().isoformat()
    sunrise = sunset = None
    for i, d in enumerate(daily.get("time", [])):
        if d == day:
            sunrise, sunset = _local(daily["sunrise"][i]), _local(daily["sunset"][i])
    if sunset is None and data.get("latitude") is not None:  # a what-if day outside the forecast
        sunrise, sunset = sun_times(at.astimezone(settings.TZ).date(), data["latitude"], data["longitude"],
                                    settings.TZ) or (None, None)
    if use_current:
        return Weather(at=at, temp_c=c["temperature_2m"], feels_c=c["apparent_temperature"], wind_ms=c["wind_speed_10m"],
                       precip_mm=c.get("precipitation", 0), snow_cm=c.get("snowfall", 0), code=c.get("weather_code", 0),
                       sunrise=sunrise, sunset=sunset, hours=hours)
    if cur is None:
        raise ValueError("no weather data")
    return Weather(at=at, temp_c=cur.temp_c, feels_c=cur.feels_c, wind_ms=cur.wind_ms, precip_mm=cur.precip_mm,
                   snow_cm=cur.snow_cm, code=cur.code, sunrise=sunrise, sunset=sunset, hours=hours)


_CACHE: dict[tuple, tuple[float, dict]] = {}


def fetch(lat: float, lon: float, ttl_s: int = 900) -> dict:
    key = (round(lat, 2), round(lon, 2))  # ~1 km: weather doesn't need exact coordinates
    params = {
        "latitude": key[0], "longitude": key[1], "timezone": settings.TZ_NAME, "wind_speed_unit": "ms",
        "forecast_days": 2,
        "current": "temperature_2m,apparent_temperature,wind_speed_10m,precipitation,snowfall,weather_code,is_day",
        "hourly": "temperature_2m,apparent_temperature,wind_speed_10m,precipitation,snowfall,weather_code",
        "daily": "sunrise,sunset",
    }
    return cached_json(_CACHE, key, OPEN_METEO, params, ttl_s)


def get(lat: float, lon: float, at: datetime, from_file: Path | None = None) -> Weather | None:
    try:
        data = json.loads(from_file.read_text(encoding="utf-8")) if from_file else fetch(lat, lon)
        w = parse(data, at)
        if from_file:
            w.source = FILE_SOURCE  # "Что я учёл: погода: из файла (демо)", not a fake "Open-Meteo"
        return w
    except Exception as e:
        log.warning("weather unavailable: %s", e)
        return None
