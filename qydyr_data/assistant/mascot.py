"""Суырбек, the mascot: which outfit fits the weather (the Mini App draws him, the bot sends him as a
sticker). Same rules as assets/mascot.js: smog > frost > rain > night > summer > hello."""
from __future__ import annotations

from datetime import datetime

from .weather import SNOW_CODES, Weather

MOODS = ("hello", "talk", "think", "happy", "celebrate", "cold", "smog", "rain", "night", "summer", "shrug")


def mascot_mood(w: Weather | None, at: datetime | None = None) -> str:
    if w is None:
        return "hello"
    if w.air is not None and w.air.european_aqi is not None and w.air.european_aqi >= 80:
        return "smog"
    if w.feels_c <= -10:
        return "cold"
    if w.precip_now and not (w.snow_cm > 0 or w.code in SNOW_CODES):
        return "rain"
    if at is not None and w.is_dark(at):
        return "night"
    if w.feels_c >= 24:
        return "summer"
    return "hello"


def weather_view(w: Weather | None, at: datetime) -> dict | None:
    """The weather as the walk screen and the mascot need it."""
    if w is None:
        return None
    level, label, factors = w.comfort(at)
    return {"temp": round(w.temp_c), "feels": round(w.feels_c), "wind": round(w.wind_ms), "level": w.level,
            "summary": w.summary(), "comfort": {"level": level, "label": label, "factors": factors},
            "max_outdoor_min": w.max_outdoor_min, "dark": w.is_dark(at), "golden_hour": w.golden_hour(at),
            "sunset": w.sunset.isoformat() if w.sunset else None, "simulated": w.simulated,
            "air": ({"aqi": w.air.european_aqi, "band": w.air.band} if w.air is not None else None),
            "mood": mascot_mood(w, at)}
