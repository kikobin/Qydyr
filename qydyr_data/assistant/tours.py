"""Прогулки с гидом: hand-made walks with sourced facts (config/tours.yaml) and the walk-mode engine.

A tour is a line of stops; each stop has a point, an arrival radius, 2–3 facts with a source link
and a tip. While you walk, the engine answers three questions: did you just reach a stop, which stop
is next and how far, and are you still on the route. The Mini App runs the same rules on the phone
(assets/walk.js), so there the exact position never leaves the device. The bot gets Telegram live
location and runs them here, in memory, one update at a time; nothing is stored.

Generated walks (assistant/walks.py) become tours too: their facts are the answer's own "why" and
"what to do", marked with the answer's sources.
"""
from __future__ import annotations

import json
import logging
import math
from collections import OrderedDict
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field

from .. import settings
from .travel import WALK_KMH, haversine_m, osrm_geometry, street_m

log = logging.getLogger(__name__)

TOURS_FILE = settings.TOURS_FILE
ARRIVE_SLACK_M = 40            # GPS in a city is off by tens of meters: forgive this much beyond the radius
OFF_ROUTE_M = 300              # farther than this from the rest of the route = "you left the route"
MASCOT = {"ru": "Суырбек", "kk": "Суырбек", "en": "Suyrbek"}
SOURCE_NAMES = {
    "wikipedia.org": "Википедия", "tengrinews.kz": "Tengrinews", "inform.kz": "Kazinform", "kazinform.kz": "Kazinform",
    "informburo.kz": "Informburo", "astana.gov.kz": "Акимат Астаны", "gov.kz": "gov.kz", "egemen.kz": "Egemen",
    "akorda.kz": "Акорда", "khanshatyr.com": "Хан Шатыр", "expo2017astana.com": "EXPO-2017", "openstreetmap.org": "OpenStreetMap",
    "sxodim.com": "sxodim", "t.me": "Telegram", "kz.kursiv.media": "Kursiv", "kursiv.media": "Kursiv", "zakon.kz": "Zakon.kz",
    "nur.kz": "NUR.KZ", "vlast.kz": "Vlast.kz", "24.kz": "24KZ", "khabar.kz": "Khabar",
}


class Text(BaseModel):
    ru: str
    kk: str | None = None
    en: str | None = None

    def get(self, lang: str = "ru") -> str:
        return (getattr(self, lang, None) or self.ru).strip()


class Fact(BaseModel):
    ru: str
    kk: str | None = None
    en: str | None = None
    source: str

    def text(self, lang: str = "ru") -> str:
        return (getattr(self, lang, None) or self.ru).strip()


class Hours(BaseModel):
    ru: str
    kk: str | None = None
    en: str | None = None
    source: str | None = None


class Stop(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9_]{1,40}$")
    name: Text
    lat: float = Field(ge=50.9, le=51.4)       # Astana
    lon: float = Field(ge=71.1, le=71.8)
    coord_source: str | None = None
    radius_m: int = Field(80, ge=30, le=200)
    outdoor: bool = True
    facts: list[Fact] = Field(min_length=1)
    tip: Text | None = None


class Tour(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9_]{1,40}$")
    title: Text
    subtitle: Text
    duration_min: int = Field(ge=10, le=300)
    distance_km: float = Field(gt=0, le=15)
    outdoor: bool = True
    best_season: Text | None = None
    hours: Hours | None = None
    stops: list[Stop] = Field(min_length=2)

    def stop(self, stop_id: str) -> Stop | None:
        return next((s for s in self.stops if s.id == stop_id), None)

    def index(self, stop_id: str) -> int:
        return next((i for i, s in enumerate(self.stops) if s.id == stop_id), -1)


# ------------------------------------------------------------------------------------------ loading
_LOADED: dict[str, tuple[float, dict[str, Tour]]] = {}


def load_tours(path: Path | None = None) -> dict[str, Tour]:
    """All curated tours by id, re-read when the file changes."""
    p = Path(path or TOURS_FILE)
    if not p.exists():
        return {}
    mtime = p.stat().st_mtime
    hit = _LOADED.get(str(p))
    if hit and hit[0] == mtime:
        return hit[1]
    raw = yaml.safe_load(p.read_text()) or {}
    items = raw.get("tours", raw) if isinstance(raw, dict) else raw
    tours = {}
    for item in items or []:
        t = Tour.model_validate(item)
        if t.id in tours:
            raise ValueError(f"duplicate tour id {t.id}")
        if len({s.id for s in t.stops}) != len(t.stops):
            raise ValueError(f"duplicate stop id in tour {t.id}")
        tours[t.id] = t
    _LOADED[str(p)] = (mtime, tours)
    return tours


def get_tour(tour_id: str) -> Tour | None:
    return load_tours().get(tour_id)


# ------------------------------------------------------------------------------------------ geometry
def legs(t: Tour) -> list[dict]:
    out = []
    for a, b in zip(t.stops, t.stops[1:]):
        m = street_m(a.lat, a.lon, b.lat, b.lon)
        out.append({"from": a.id, "to": b.id, "distance_m": round(m), "walk_min": max(1, round(m / 1000 / WALK_KMH * 60)),
                    "outdoor": True})
    return out


def geometry(t: Tour) -> tuple[list[list[float]], str]:
    pts = [[s.lon, s.lat] for s in t.stops]
    street = osrm_geometry(pts)
    return (street, "osrm") if street else (pts, "straight")


def _seg_dist_m(lat: float, lon: float, a: tuple[float, float], b: tuple[float, float]) -> float:
    """Distance from a point to the segment a-b, on a local flat projection (fine at city scale)."""
    k = math.cos(math.radians(lat))
    px, py = lon * k, lat
    ax, ay, bx, by = a[1] * k, a[0], b[1] * k, b[0]
    dx, dy = bx - ax, by - ay
    t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    cx, cy = ax + t * dx, ay + t * dy
    return haversine_m(lat, lon, cy, cx / k)


def route_distance_m(lat: float, lon: float, points: list[tuple[float, float]]) -> float:
    if not points:
        return float("inf")
    if len(points) == 1:
        return haversine_m(lat, lon, *points[0])
    return min(_seg_dist_m(lat, lon, a, b) for a, b in zip(points, points[1:]))


# ------------------------------------------------------------------------------------------ the walk
def next_stop(t: Tour, visited: Iterable[str]) -> Stop | None:
    """The first unvisited stop after the furthest one reached; stops skipped earlier come last."""
    seen = set(visited)
    reached = [t.index(s) for s in seen if t.index(s) >= 0]
    start = max(reached) + 1 if reached else 0
    order = t.stops[start:] + t.stops[:start]
    return next((s for s in order if s.id not in seen), None)


def progress(t: Tour, lat: float, lon: float, visited: Iterable[str] = (), accuracy_m: float | None = None) -> dict:
    """One location update. Returns what changed: the stop just reached (if any), the next stop with
    its distance and walking time, whether the walk is done and whether you are off the route."""
    seen = [s for s in dict.fromkeys(visited) if t.stop(s)]
    slack = ARRIVE_SLACK_M + min(max(accuracy_m or 0, 0), 60)
    near = [(haversine_m(lat, lon, s.lat, s.lon), s) for s in t.stops if s.id not in seen]
    near = [(d, s) for d, s in near if d <= s.radius_m + slack]
    arrived = min(near, key=lambda x: x[0])[1] if near else None
    if arrived:
        seen.append(arrived.id)
    nxt = next_stop(t, seen)
    out = {"arrived": arrived.id if arrived else None, "visited": seen, "done": nxt is None,
           "next": nxt.id if nxt else None, "next_m": None, "next_min": None, "off_route": False}
    if nxt:
        m = street_m(lat, lon, nxt.lat, nxt.lon)
        out["next_m"] = round(m)
        out["next_min"] = max(1, round(m / 1000 / WALK_KMH * 60)) if m > 60 else 0
        if seen:   # before the first stop you are on your way to the start, not off the route
            last = t.stops[max(t.index(s) for s in seen)]
            rest = [(s.lat, s.lon) for s in t.stops[t.index(nxt.id):]]
            if t.index(last.id) < t.index(nxt.id):
                rest = [(last.lat, last.lon)] + rest
            out["off_route"] = route_distance_m(lat, lon, rest) > OFF_ROUTE_M
    return out


# ------------------------------------------------------------------------------------------ views
def source_name(url: str) -> str:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    for domain, name in SOURCE_NAMES.items():
        if host == domain or host.endswith("." + domain):
            return name
    return host or url


def _plural_ru(n: int, one: str, few: str, many: str) -> str:
    d, h = n % 10, n % 100
    return one if d == 1 and h != 11 else few if 2 <= d <= 4 and not 12 <= h <= 14 else many


def meta_line(t: Tour, lang: str = "ru") -> str:
    n, km = len(t.stops), f"{t.distance_km:.1f}".replace(".", ",") if lang != "en" else f"{t.distance_km:.1f}"
    if lang == "kk":
        return f"{n} нүкте · {t.duration_min} мин · {km} км"
    if lang == "en":
        return f"{n} stops · {t.duration_min} min · {km} km"
    return f"{n} {_plural_ru(n, 'точка', 'точки', 'точек')} · {t.duration_min} мин · {km} км"


def intro_line(t: Tour, lang: str = "ru") -> str:
    facts = sum(len(s.facts) for s in t.stops)
    name = MASCOT.get(lang, MASCOT["ru"])
    if lang == "kk":
        return f"Мен {name}. «{t.title.get('kk')}» бойымен апарамын: {len(t.stops)} нүкте, {facts} қызық дерек."
    if lang == "en":
        return f"I'm {name}. Let's walk “{t.title.get('en')}”: {len(t.stops)} stops, {facts} facts on the way."
    return (f"Я {name}. Проведу по маршруту «{t.title.get('ru')}»: {len(t.stops)} "
            f"{_plural_ru(len(t.stops), 'точка', 'точки', 'точек')}, {facts} "
            f"{_plural_ru(facts, 'факт', 'факта', 'фактов')} по дороге.")


def _translated(strings: list[str], lang: str, llm=None) -> list[str] | None:
    if lang == "ru" or not strings:
        return None
    try:
        from .translate import translate_strings
        return translate_strings(strings, lang, llm=llm)
    except Exception as e:  # translation is a nicety, never a failure
        log.warning("tour translation failed: %s", e)
        return None


def stop_view(t: Tour, s: Stop, lang: str = "ru") -> dict:
    return {
        "id": s.id, "n": t.index(s.id) + 1, "name": s.name.get(lang), "lat": s.lat, "lon": s.lon,
        "radius_m": s.radius_m, "outdoor": s.outdoor,
        "facts": [{"text": f.text(lang), "source": f.source, "source_name": source_name(f.source),
                   "lang": lang if getattr(f, lang, None) else "ru"} for f in s.facts],
        "tip": s.tip.get(lang) if s.tip else None,
    }


def tour_card(t: Tour, lang: str = "ru") -> dict:
    lat = sum(s.lat for s in t.stops) / len(t.stops)
    lon = sum(s.lon for s in t.stops) / len(t.stops)
    return {"id": t.id, "kind": "tour", "title": t.title.get(lang), "subtitle": t.subtitle.get(lang),
            "meta": meta_line(t, lang), "stops": len(t.stops), "facts": sum(len(s.facts) for s in t.stops),
            "duration_min": t.duration_min, "distance_km": t.distance_km, "outdoor": t.outdoor,
            "center": [round(lat, 5), round(lon, 5)], "start": [t.stops[0].lat, t.stops[0].lon]}


def tour_view(t: Tour, lang: str = "ru", llm=None) -> dict:
    stops = [stop_view(t, s, lang) for s in t.stops]
    # facts without a hand-written translation: Claude translates them when a key is set, else Russian stays
    todo = [f for st in stops for f in st["facts"] if f["lang"] != lang]
    if todo:
        tr = _translated([f["text"] for f in todo], lang, llm)
        if tr:
            for f, x in zip(todo, tr):
                f["text"], f["lang"], f["translated"] = x, lang, True
    geo, src = geometry(t)
    return {**tour_card(t, lang), "intro": intro_line(t, lang), "stops_list": stops, "legs": legs(t),
            "geometry": geo, "geometry_source": src,
            "best_season": t.best_season.get(lang) if t.best_season else None,
            "hours": ({"text": getattr(t.hours, lang, None) or t.hours.ru, "source": t.hours.source} if t.hours else None),
            "mascot": MASCOT.get(lang, MASCOT["ru"])}


def tour_cards(lang: str = "ru") -> list[dict]:
    return [tour_card(t, lang) for t in load_tours().values()]


# ------------------------------------------------------------------------------------------ the story
LANG_NAMES = {"ru": "русском", "kk": "казахском", "en": "английском"}
STORY_SYSTEM = ("Ты — Суырбек, степной сурок в ақ қалпақ и гид Qydyr по Астане. Человек стоит у точки маршрута. "
                "Расскажи ему 3–5 коротких предложений (50–80 слов), живо и по-доброму, как друг-экскурсовод. "
                "Используй ТОЛЬКО факты из блока FACTS: не добавляй цифры, даты, имена и легенды, которых там нет. "
                "Легенду называй легендой. Закончи тем, что сделать прямо здесь (из tip). Пиши на {lang} языке, без эмодзи, "
                "без списков, без восклицательных знаков.")
_STORIES: "OrderedDict[tuple[str, str, str], dict]" = OrderedDict()


def plain_story(t: Tour, s: Stop, lang: str = "ru") -> str:
    parts = [f.text(lang).rstrip(".") + "." for f in s.facts]
    if s.tip:
        parts.append(s.tip.get(lang).rstrip(".") + ".")
    return " ".join(parts)


def stop_story(t: Tour, s: Stop, lang: str = "ru", llm=None) -> dict:
    """The mascot's spoken story at a stop: Claude retells the stop's facts, or the facts as they are."""
    key = (t.id, s.id, lang, hash(tuple(f.ru for f in s.facts)))   # an edited tours.yaml retells the new facts
    if key in _STORIES:
        return _STORIES[key]
    facts = {"place": s.name.get("ru"), "facts": [f.ru for f in s.facts], "tip": s.tip.ru if s.tip else ""}
    out = {"text": plain_story(t, s, lang), "source": "facts", "sources": sorted({source_name(f.source) for f in s.facts})}
    if llm is not None:
        try:
            text = llm.text(system=STORY_SYSTEM.format(lang=LANG_NAMES.get(lang, "русском")),
                            user="FACTS:\n" + json.dumps(facts, ensure_ascii=False), max_tokens=400)
            if text and text.strip():
                out = {**out, "text": text.strip(), "source": "claude"}
        except Exception as e:
            log.warning("stop story via Claude failed: %s", e)
    _STORIES[key] = out
    if len(_STORIES) > 500:
        _STORIES.popitem(last=False)
    return out


# ------------------------------------------------------------------------------------------ generated walks
def tour_from_answer(walk: dict, lang: str = "ru") -> dict:
    """A generated themed walk (assistant/walks.py) in the same shape as tour_view(): its stops are the
    answer's route, its facts are the answer's own reasons, marked as such."""
    ans = walk["answer"]
    items = {i.get("place_id") or i["title"]: i for i in ans.get("items", [])}
    stops_src = (ans.get("route") or {}).get("stops") or [
        {"title": i["title"], "lat": i["lat"], "lon": i["lon"], "place_id": i.get("place_id")} for i in ans.get("items", [])]
    stops = []
    for n, s in enumerate(stops_src, 1):
        it = items.get(s.get("place_id") or s["title"]) or {}
        lines = [x for x in [it.get("status"), *it.get("why", [])] if x]
        facts = [{"text": x[0].upper() + x[1:], "source": "", "source_name": ", ".join(it.get("sources", [])) or "Qydyr",
                  "lang": lang} for x in lines[:3]]
        stops.append({"id": f"s{n}", "n": n, "name": s["title"], "lat": s.get("lat") or it.get("lat"),
                      "lon": s.get("lon") or it.get("lon"), "radius_m": 70, "outdoor": it.get("indoor") != "indoor",
                      "facts": facts, "tip": it.get("what_to_do") or None, "place_id": s.get("place_id")})
    stops = [s for s in stops if s["lat"] is not None]
    pts = [[s["lon"], s["lat"]] for s in stops]
    return {"id": f"walk_{walk['id']}", "kind": "walk", "title": walk["title"], "subtitle": walk["subtitle"],
            "meta": walk["meta"], "stops": len(stops), "facts": sum(len(s["facts"]) for s in stops),
            "duration_min": (ans.get("route") or {}).get("total_min"), "distance_km": None, "outdoor": True,
            "center": pts[0][::-1] if pts else None, "start": pts[0][::-1] if pts else None,
            "intro": walk["subtitle"], "stops_list": stops, "legs": [], "geometry": pts, "geometry_source": "straight",
            "best_season": None, "hours": None, "mascot": MASCOT.get(lang, MASCOT["ru"])}


# ------------------------------------------------------------------------------------------ nudges on the way
NUDGE = {
    "now": {"ru": "Рядом прямо сейчас: {title} — {venue}, {d} м.",
            "kk": "Қазір жақын жерде: {title} — {venue}, {d} м.",
            "en": "On right now nearby: {title} at {venue}, {d} m away."},
    "soon": {"ru": "Через {m} мин в {d} м начнётся {title} — {venue}.",
             "kk": "{m} минуттан кейін {d} м жерде {title} басталады — {venue}.",
             "en": "In {m} min, {d} m away: {title} at {venue}."},
    "warm": {"ru": "До следующей точки {leg} мин по улице, а при {feels}° комфортно не больше {cap} мин. "
                   "По пути можно погреться: {name}, {walk} мин.",
             "kk": "Келесі нүктеге дейін көшемен {leg} мин, ал {feels}° кезінде {cap} минуттан артық жүру жайсыз. "
                   "Жолда жылынуға болады: {name}, {walk} мин.",
             "en": "The next stop is {leg} min away outside, and at {feels}° about {cap} min is comfortable. "
                   "Warm up on the way: {name}, {walk} min."},
    "air": {"ru": "Воздух сейчас плохой: уличную часть маршрута лучше сократить или перенести.",
            "kk": "Қазір ауа сапасы нашар: маршруттың көшедегі бөлігін қысқартқан не кейінге қалдырған дұрыс.",
            "en": "The air is bad right now: keep the outdoor part short or come back another time."},
}


def nudges(events: list[dict], *, weather=None, next_leg_min: int | None = None, warm: list[dict] | None = None,
           lang: str = "ru", limit: int = 3) -> list[dict]:
    """Short hints while walking: an event on now or starting soon right next to you, a place to warm up
    when the next leg is longer than is comfortable in this frost, a warning about bad air."""
    out: list[dict] = []
    for e in events:
        d, venue = int(e.get("distance_m") or 0), e.get("venue") or ""
        title = e.get("title") or ""
        if "«" not in title and "“" not in title:   # names often carry their own quotes: never double them
            title = f"«{title}»" if lang != "en" else f"“{title}”"
        if e.get("state") == "now" and d <= 250:
            out.append({"kind": "event", "event_id": e.get("event_id"), "place_id": e.get("place_id"),
                        "lat": e.get("lat"), "lon": e.get("lon"),
                        "text": NUDGE["now"][lang].format(title=title, venue=venue, d=d)})
        elif e.get("state") == "soon" and (e.get("minutes_to_start") or 999) <= 45 and d <= 450:
            out.append({"kind": "event", "event_id": e.get("event_id"), "place_id": e.get("place_id"),
                        "lat": e.get("lat"), "lon": e.get("lon"),
                        "text": NUDGE["soon"][lang].format(title=title, venue=venue, d=d, m=e["minutes_to_start"])})
    if weather is not None and next_leg_min and next_leg_min > weather.max_outdoor_min and warm:
        p = warm[0]
        walk = max(1, round((p.get("distance_m") or 0) * 1.3 / 1000 / WALK_KMH * 60))
        name = p.get(f"name_{lang}") or p.get("name_ru") or p.get("name")
        out.append({"kind": "warm", "place_id": p.get("place_id"), "lat": p.get("lat"), "lon": p.get("lon"),
                    "text": NUDGE["warm"][lang].format(leg=next_leg_min, feels=f"{weather.feels_c:+.0f}",
                                                       cap=weather.max_outdoor_min, name=name, walk=walk)})
    air = getattr(weather, "air", None) if weather is not None else None
    if air is not None and air.european_aqi is not None and air.european_aqi >= 80:
        out.append({"kind": "air", "text": NUDGE["air"][lang]})
    return out[:limit]
