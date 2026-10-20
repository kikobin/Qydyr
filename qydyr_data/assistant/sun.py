"""Sunrise and sunset without any API (NOAA solar position approximation, ±2 min at Astana's latitude).
Used when there is no weather data and for what-if times: "закат в 17:52", "золотой час"."""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone, tzinfo


def sun_times(d: date, lat: float, lon: float, tz: tzinfo) -> tuple[datetime, datetime] | None:
    """(sunrise, sunset) in `tz`, or None during polar day/night."""
    n = d.timetuple().tm_yday
    g = 2 * math.pi / 365 * (n - 1)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    phi = math.radians(lat)
    cos_ha = math.cos(math.radians(90.833)) / (math.cos(phi) * math.cos(decl)) - math.tan(phi) * math.tan(decl)
    if not -1.0 <= cos_ha <= 1.0:
        return None
    ha = math.degrees(math.acos(cos_ha))
    base = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    rise = base + timedelta(minutes=720 - 4 * (lon + ha) - eqtime)
    sset = base + timedelta(minutes=720 - 4 * (lon - ha) - eqtime)
    return rise.astimezone(tz), sset.astimezone(tz)
