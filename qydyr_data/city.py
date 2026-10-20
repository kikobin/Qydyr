"""Qydyr for the city: where Astana cannot answer typical requests, and what people look for and do not find.

Supply audit. The assistant itself asks a handful of typical requests ("с ребёнком вечером", "тихо поработать
с розеткой", "на коляске", "ночью"...) from the centre of every populated ~1 km cell, on the real data, at a
fixed moment. A cell where nothing fits (or the nearest fit is far) is a gap: a fact about the city, not about a
user. Live demand. Real requests, kept only as counts per day, ~1 km cell and need, and shown only after
DEMAND_K requests. Both feed the "Qydyr для города" page (/app/city.html)."""
from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import yaml

from . import settings, store
from .assistant import knowledge as K
from .assistant import travel
from .assistant.privacy import cell_center, geohash

log = logging.getLogger(__name__)

CELL_PRECISION = 6          # ~1.2 x 0.6 km, the same cell as the pulse and the demand counts
MIN_PLACES = 3              # an empty steppe cell is not a gap
FAR_MIN = 15                # the nearest fit further than this is "далеко"
# how a cell is named: the nearest well-known place, the bigger kinds preferred (a mall says more than a statue)
LANDMARK_WEIGHT = {"mall": 0.6, "park": 0.7, "university": 0.7, "stadium": 0.8, "museum": 0.8, "theatre": 0.8,
                   "concert_hall": 0.8, "exhibition_centre": 0.8, "railway_station": 0.9, "monument": 1.0,
                   "attraction": 1.0, "place_of_worship": 1.1}


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str                  # what the map shows: "С ребёнком вечером"
    text: str                   # what the assistant is asked, as a person would
    weekday: int                # Monday = 0
    at: time
    must: tuple[str, ...]       # the needs an answer has to cover (yes / likely) to count
    preset: str | None = None   # what-if weather
    why: str = ""               # one line for the city: why this request matters
    strict: bool = False        # only what the data confirms counts as covered ("likely" is shown as a guess)


SCENARIOS = [
    Scenario("kids_evening", "С ребёнком вечером", "с ребёнком вечером, под крышей, есть час", 4, time(18, 30),
             ("kids", "indoor"), why="Семьям после работы и сада: в октябре в 18:30 уже темно, нужно место под крышей."),
    Scenario("work_quiet", "Тихо поработать", "хочу тихо поработать: кофе и розетка, пара часов", 1, time(15, 0),
             ("coffee", "socket"), why="Студентам и фрилансерам: кафе и коворкинги с розеткой в шаговой доступности."),
    Scenario("step_free", "На коляске", "я на коляске, хочу погулять час", 5, time(13, 0), ("accessible",),
             why="Людям на колясках и с детскими колясками: места, где доступность отмечена в данных.", strict=True),
    Scenario("night", "Ночью", "что открыто ночью, пара часов", 4, time(23, 30), ("night",),
             why="Ночной жизни и сменным работникам: что работает после 23:00."),
    Scenario("frost", "Погреться в мороз", "мороз, хочу погреться и поесть, час", 1, time(13, 0), ("indoor", "food"),
             preset="frost", why="Зиме в Астане: тёплое место с едой в паре минут, когда на улице −20."),
    Scenario("events", "Что идёт вечером", "что идёт сегодня вечером, пара часов", 4, time(19, 0), ("event",),
             why="Культурной жизни районов: события, до которых можно дойти пешком."),
]
BY_ID = {s.id: s for s in SCENARIOS}


def reference_day(today: date | None = None) -> date:
    """The week the audit speaks about: the demo's frozen week, or the coming one."""
    if settings.DEMO_NOW:
        return datetime.fromisoformat(settings.DEMO_NOW).date()
    return today or datetime.now(settings.TZ).date()


def moment(s: Scenario, day: date) -> datetime:
    """The next `s.weekday` on or after `day`, at `s.at`, local time."""
    d = day + timedelta(days=(s.weekday - day.weekday()) % 7)
    return datetime.combine(d, s.at, tzinfo=settings.TZ)


def cells(places: list[dict]) -> dict[str, list[dict]]:
    """Populated ~1 km cells: the places in each (transport stops don't make a neighbourhood)."""
    out: dict[str, list[dict]] = {}
    for p in places:
        if p["category"] != "anchor":
            out.setdefault(geohash(p["lat"], p["lon"], CELL_PRECISION), []).append(p)
    return {c: ps for c, ps in out.items() if len(ps) >= MIN_PLACES}


GENERIC = re.compile(r"площадк|парковк|остановк|стоянк|туалет|банкомат|^\W*$", re.I)


def label(lat: float, lon: float, places: list[dict], here: list[dict] | None = None) -> str | None:
    """How a cell reads as a part of the city: the nearest well-known place within ~1.2 km ("Mega Silk Way"),
    else the street most of its places are on."""
    best = None
    for p in places:
        w = LANDMARK_WEIGHT.get(p["subcategory"])
        if w is None or not p["name"]:
            continue
        d = travel.haversine_m(lat, lon, p["lat"], p["lon"])
        if d <= 1200 and (best is None or d * w < best[0]):
            best = (d * w, p["name_ru"] or p["name"])
    if best:
        return best[1]
    streets = Counter(st for p in here or [] if (st := (p.get("address") or "").split(",")[0].strip())
                      and sum(ch.isalpha() for ch in st) >= 3)
    if streets:
        return streets.most_common(1)[0][0]
    # no landmark, no street: the place people would know, not a generic "Детская площадка"
    named = [n for p in here or [] if (n := p.get("name_ru") or p.get("name")) and not GENERIC.search(n)]
    if named:
        return f"у «{named[0]}»"
    near = min(((travel.haversine_m(lat, lon, p["lat"], p["lon"]), n) for p in places
                if (n := p["name_ru"] or p["name"]) and not GENERIC.search(n)), default=None)
    return f"около «{near[1]}»" if near and near[0] <= 3000 else None


def _fits(item, must: tuple[str, ...], sure: bool = False) -> bool:
    return all(item.need_hits.get(n) in (("yes",) if sure else ("yes", "likely")) for n in must)


def run_audit(conn, scenarios: list[Scenario] | None = None, day: date | None = None) -> dict[str, int]:
    """Ask every scenario in every populated cell and store what fits. Returns gaps per scenario."""
    from .assistant.recommend import Assistant, DbData
    rows = conn.execute(
        """select id, name, name_ru, category, subcategory, address, st_y(geom::geometry) lat, st_x(geom::geometry) lon
           from places""").fetchall()
    grid = cells(rows)
    assistant = Assistant(DbData(conn))
    day = reference_day(day)
    gaps: dict[str, int] = {}
    for s in scenarios or SCENARIOS:
        at = moment(s, day)
        out = []
        for cell, ps in grid.items():
            lat, lon = cell_center(cell)
            ans = assistant.ask(s.text, lat=lat, lon=lon, at=at, fetch_weather=False, weather_preset=s.preset)
            fit = [i for i in ans.items if _fits(i, s.must)]
            out.append({"cell": cell, "lat": round(lat, 5), "lon": round(lon, 5), "label": label(lat, lon, rows, ps),
                        "places": len(ps), "options": len(fit), "confirmed": sum(1 for i in fit if _fits(i, s.must, sure=True)),
                        "nearest_min": min((i.walk_min for i in fit), default=None),
                        "best": [i.title for i in fit[:2]], "at": at})
        store.replace_audit(conn, s.id, out)
        gaps[s.id] = sum(1 for r in out if level(r, s) != "ok")
        log.info("audit %s: %d cells, %d gaps", s.id, len(out), gaps[s.id])
    return gaps


def level(r: dict, s: Scenario) -> str:
    """none: nothing fits; guess: only "likely" fits where the data must confirm; far: the nearest fit is far."""
    if not r["options"]:
        return "none"
    if s.strict and not r.get("confirmed"):
        return "guess"
    return "far" if (r["nearest_min"] or 0) > FAR_MIN else "ok"


def gaps_view(conn, scenario: str) -> dict:
    s = BY_ID[scenario]
    rows = store.audit(conn, scenario)
    for r in rows:
        r["level"] = level(r, s)
    # a gap matters where people are: the busiest cells first, one line per named part of the city
    order = {"none": 0, "guess": 1, "far": 2}
    top, seen = [], set()
    for r in sorted((r for r in rows if r["level"] != "ok"), key=lambda r: (order[r["level"]], -r["places"])):
        if r["label"] and r["label"] in seen:
            continue
        seen.add(r["label"])
        top.append(r)
    top = top[:6]
    n = Counter(r["level"] for r in rows)
    return {"scenario": {"id": s.id, "title": s.title, "request": s.text, "why": s.why,
                         "at": rows[0]["at"].isoformat() if rows else None, "run_at": rows[0]["run_at"].isoformat() if rows else None},
            "cells": [{k: r[k] for k in ("cell", "lat", "lon", "label", "places", "options", "confirmed", "nearest_min",
                                         "best", "level")} for r in rows],
            "summary": {"cells": len(rows), "none": n["none"], "guess": n["guess"], "far": n["far"], "ok": n["ok"]},
            "top": [{k: r[k] for k in ("cell", "lat", "lon", "label", "places", "options", "confirmed", "nearest_min", "best",
                                       "level")}
                    for r in top]}


def scenarios_view(conn=None) -> list[dict]:
    """Every scenario, with how the city does on it when the audit has run (for the cards on the city page)."""
    out = []
    for s in SCENARIOS:
        row = {"id": s.id, "title": s.title, "request": s.text, "why": s.why}
        if conn is not None:
            n = Counter(level(r, s) for r in store.audit(conn, s.id))
            row["summary"] = {"cells": sum(n.values()), "none": n["none"], "guess": n["guess"], "far": n["far"], "ok": n["ok"]}
        out.append(row)
    return out


def unmet_needs(intent, items) -> tuple[list[str], list[str]]:
    """What the person asked for (not what we assumed) and which of it no answer covered."""
    assumed = any(a.startswith("просто погулять") for a in intent.assumptions)
    needs = [] if assumed else [n for n in intent.needs if n in K.NEED_LABELS]
    unmet = [n for n in needs if not any(i.need_hits.get(n) in ("yes", "likely") for i in items)]
    return needs, unmet


def demand_view(conn, live: list[dict] | None = None) -> dict:
    rows = store.demand_summary(conn)
    cells_: dict[str, dict] = {}
    for r in rows:
        lat, lon = cell_center(r["cell"])
        c = cells_.setdefault(r["cell"], {"cell": r["cell"], "lat": round(lat, 4), "lon": round(lon, 4),
                                          "asked": 0, "unmet": 0, "needs": []})
        c["asked"] += r["asked"]
        c["unmet"] += r["unmet"]
        c["needs"].append({"need": r["need"], "label": K.NEED_LABELS.get(r["need"], r["need"]),
                           "asked": r["asked"], "unmet": r["unmet"]})
    by_need = Counter()
    asked_need = Counter()
    for r in rows:
        by_need[r["need"]] += r["unmet"]
        asked_need[r["need"]] += r["asked"]
    live = [dict(x) for x in live or []]
    want = list({*cells_, *(x["cell"] for x in live)})
    names = {r["cell"]: r["label"] for r in conn.execute(
        "select distinct on (cell) cell, label from city_audit where cell = any(%s) and label is not null", (want,))
    } if want else {}
    for c in [*cells_.values(), *live]:   # "у Хан Шатыра" reads better than a geohash
        c["label"] = names.get(c["cell"])
    return {"k": store.DEMAND_K, "days": 30,
            "cells": sorted(cells_.values(), key=lambda c: -c["unmet"]),
            "needs": [{"need": n, "label": K.NEED_LABELS.get(n, n), "unmet": u, "asked": asked_need[n]}
                      for n, u in by_need.most_common() if u],
            "live": live}


def data_view(conn) -> dict:
    """Where every answer comes from: sources, freshness, coverage — and what is missing."""
    from .assistant import tours as TOURS
    from .events.crawl import load_sources
    st = store.data_stats(conn)
    db_sources = {r["id"]: r for r in st["sources"]}
    per_source = Counter()
    for r in conn.execute("select jsonb_array_elements(sources)->>'source_id' sid from events").fetchall():
        per_source[r["sid"]] += 1
    sources = [{"id": "osm", "kind": "osm", "title": "OpenStreetMap (© участники OSM, ODbL)",
                "url": "https://www.openstreetmap.org", "items": st["places"]["n"], "unit": "мест",
                "last_run_at": (db_sources.get("osm") or {}).get("last_run_at"), "status": (db_sources.get("osm") or {}).get("last_status")}]
    if p_enr := st["places"]["dgis_enriched"] + st["places"]["dgis_new"]:
        db = db_sources.get("2gis") or {}
        sources.append({"id": "2gis", "kind": "catalog", "title": "2ГИС (© 2ГИС)", "url": "https://2gis.kz/astana",
                        "items": p_enr, "unit": f"мест: {st['places']['dgis_enriched']} дополнено, {st['places']['dgis_new']} новых",
                        "last_run_at": db.get("last_run_at"), "status": db.get("last_status")})
    for s in load_sources():
        if not s.enabled:
            continue
        db = db_sources.get(s.id) or {}
        sources.append({"id": s.id, "kind": s.kind, "title": s.title or s.id, "url": s.url, "items": per_source[s.id],
                        "unit": "событий", "last_run_at": db.get("last_run_at"), "status": db.get("last_status")})
    head = 2 if len(sources) > 1 and sources[1]["id"] == "2gis" else 1
    sources = sources[:head] + sorted(sources[head:], key=lambda x: -x["items"])
    tours = TOURS.tour_cards("ru")
    all_tours = list(TOURS.load_tours().values())
    facts = sum(len(st_.facts) for t in all_tours for st_ in t.stops)
    overrides = yaml.safe_load((settings.CONFIG_DIR / "place_overrides.yaml").read_text(encoding="utf-8")) or {}
    return {
        "places": st["places"], "places_by_category": st["places_by_category"], "events": st["events"],
        "sessions_7d": st["sessions_7d"], "sources": sources,
        "curated": {"places": len(overrides), "with_hours": sum(1 for r in overrides.values() if (r or {}).get("hours_source"))},
        "tours": {"n": len(tours), "stops": sum(len(t.stops) for t in all_tours), "facts": facts},
        "schedule": [{"what": "Места из OpenStreetMap", "when": "раз в сутки"},
                     {"what": "События: афиши, сайты площадок, Telegram", "when": "каждые 6 часов"},
                     {"what": "Аудит города", "when": "раз в сутки, после сбора"}],
        "weather": "Open-Meteo (погода), Open-Meteo Air Quality / CAMS (воздух) — на каждый запрос",
    }
