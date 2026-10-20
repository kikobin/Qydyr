"""Mini-route: 2–3 stops in an order that fits the time budget, respects opening hours and event
start times, keeps time outside short in the cold, and doesn't repeat the same kind of place."""
from __future__ import annotations

from datetime import datetime, timedelta
from itertools import permutations
from typing import TYPE_CHECKING

from . import travel
from .models import Anchor, Route, Stop
from .weather import Weather

if TYPE_CHECKING:
    from .recommend import Scored


def _mins(td: timedelta) -> int:
    return int(td.total_seconds() // 60)


def build_route(scored: list["Scored"], anchor: Anchor, at: datetime, end: datetime, *, kids: bool = False,
                wheelchair: bool = False, weather: Weather | None = None) -> Route | None:
    pool = [s for s in scored if s.visit[0] <= 60][:8]  # a 2-hour show is not a micro-route stop
    for k in (3, 2):
        best = None
        for perm in permutations(pool, k):
            if len({"food" if p.cand.category == "food" else p.cand.subcategory for p in perm}) < k:
                continue  # two cafés (or a café, then a restaurant) in a row is not a walk
            t, lat, lon = at, anchor.lat, anchor.lon
            stops: list[Stop] = []
            walk_total = outdoor = 0
            score, ok = 0.0, True
            for s in perm:
                w = travel.walk_min(lat, lon, s.cand.lat, s.cand.lon, kids=kids, wheelchair=wheelchair, weather=weather)
                start = max(t + timedelta(minutes=w), s.start_at or t)
                stay = s.visit[0] if k == 3 else (s.visit[0] + s.visit[1]) // 2
                if s.window_end is not None:
                    room = _mins(s.window_end - start)
                    if room < min(stay, 15):
                        ok = False
                        break
                    stay = min(stay, room)
                leave = start + timedelta(minutes=stay)
                pid = s.cand.id if s.cand.kind == "place" else (s.cand.event or {}).get("place_id")
                stops.append(Stop(title=s.cand.title, arrive_at=start, leave_at=leave, walk_min=w, lat=s.cand.lat,
                                  lon=s.cand.lon, place_id=pid, kind=s.cand.kind))
                walk_total += w
                outdoor += w + (stay if s.cand.indoor == "outdoor" else 0)
                score += s.score
                t, lat, lon = leave, s.cand.lat, s.cand.lon
            if not ok or t > end:
                continue
            if weather is not None and outdoor > weather.max_outdoor_min * 1.5:
                continue  # too long outside for this weather
            score -= 0.004 * walk_total
            if best is None or score > best[0]:
                best = (score, stops, walk_total, outdoor, t)
        if best:
            _, stops, walk_total, outdoor, t_end = best
            points = [[anchor.lon, anchor.lat]] + [[s.lon, s.lat] for s in stops]
            street = travel.osrm_geometry(points)  # exact street path when a router is configured
            return Route(stops=stops, total_min=_mins(t_end - at), walk_min=walk_total, outdoor_min=outdoor,
                         geometry=street or points, geometry_source="osrm" if street else "straight")
    return None
