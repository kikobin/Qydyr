"""Афиша: what is on in the city in the next days, day by day — for the Mini App's "Афиша" tab and the bot's
/afisha. One row per event and day with all its times that day; the same show listed by two afishas is shown once."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from . import settings
from .assistant import travel
from .assistant.knowledge import _GLUED

KINDS = ["concert", "performance", "standup", "exhibition", "kids", "film", "excursion", "party", "festival",
         "workshop", "lecture", "fair", "sport", "other"]
# the chips people filter by: a few kinds read as one ("театр" covers performances and musicals)
GROUPS = {"concert": ["concert", "festival", "party"], "theatre": ["performance"], "standup": ["standup"],
          "exhibition": ["exhibition", "fair"], "kids": ["kids"], "film": ["film"],
          "excursion": ["excursion", "workshop", "lecture"]}
PER_DAY = 60


def _key(title: str) -> str:
    """"Мультимедийная выставка «Айвазовский. Ожившие полотна»" on Ticketon and on Sxodim is one show."""
    t = re.sub(r"[«»\"'“”]", "", title.lower())
    t = re.sub(r"^(мультимедийная выставка|выставка|концерт|спектакль|стендап|stand[- ]?up)\s+", "", t)
    return re.sub(r"\W+", "", t)[:40]


def _price(text: str | None) -> str | None:
    """"От 12 750 ₸ во Freedom (по промокоду…); от 15 000 ₸ на сайте" -> "От 12 750 ₸": the first price is enough."""
    if not text:
        return None
    first = re.split(r"[;(]| во | на сайте", text)[0].strip(" ,.")
    return first[:40] or None


def _clean(text: str | None) -> str | None:
    """"Сбор: 18:00Начало: 19:00Выступят: …" as ticket sites glue it, cut at a sentence near 280 characters."""
    if not text:
        return None
    t = re.sub(r"(?<=[а-яё.!?»)])(?=[А-ЯЁ])", " ", _GLUED.sub(r"\1 · ", text.strip()))
    if len(t) <= 280:
        return t
    cut = t[:280]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return (cut[:end + 1] if end > 120 else cut.rsplit(" ", 1)[0] + "…")


def afisha(conn, at: datetime, days: int = 7, group: str | None = None, lat: float | None = None,
           lon: float | None = None) -> dict:
    tz = settings.TZ
    at = at.astimezone(tz)
    start_day = at.date()
    end = datetime.combine(start_day + timedelta(days=days), datetime.min.time(), tzinfo=tz)
    kinds = GROUPS.get(group) if group else None
    rows = conn.execute(
        """select e.id, e.title, e.kind, e.venue_name, e.place_id, e.is_free, e.price_text, e.url, e.image_url,
                  e.description, e.age_limit, e.ends_on, e.is_temporary,
                  st_y(e.geom::geometry) lat, st_x(e.geom::geometry) lon,
                  s.starts_at, s.ends_at, s.time_known
           from events e join event_sessions s on s.event_id = e.id
           where e.status = 'scheduled' and s.ends_at > %s and s.starts_at < %s
           order by s.starts_at""", (at, end)).fetchall()
    by_day: dict = {}
    counts: dict[str, int] = {}
    seen: dict = {}
    counted: set = set()
    for r in rows:
        s = r["starts_at"].astimezone(tz)
        day = max(s.date(), start_day)
        k = (day, _key(r["title"]))
        g = next((g for g, ks in GROUPS.items() if r["kind"] in ks), "other")
        if k[1] not in counted:   # the chips count each show once, whatever kind is picked
            counted.add(k[1])
            counts[g] = counts.get(g, 0) + 1
        if kinds and r["kind"] not in kinds:
            continue
        hm = s.strftime("%H:%M") if r["time_known"] and s.date() == day else None
        if k in seen:     # one more time the same day, or the same show from another afisha
            ev = seen[k]
            if hm and hm not in ev["times"]:
                ev["times"].append(hm)
            for f, col in (("image", "image_url"), ("lat", "lat"), ("lon", "lon"), ("place_id", "place_id"), ("url", "url")):
                if ev[f] is None and r[col] is not None:
                    ev[f] = r[col]
            continue
        if len(by_day.setdefault(day, [])) >= PER_DAY:
            continue
        now = r["starts_at"] <= at < r["ends_at"]
        ev = {"id": r["id"], "title": r["title"], "kind": r["kind"], "venue": r["venue_name"], "place_id": r["place_id"],
              "date": day.isoformat(), "times": [hm] if hm else [], "now": now,
              "ends_at": r["ends_at"].astimezone(tz).strftime("%H:%M") if now else None,
              "free": bool(r["is_free"]), "price": _price(r["price_text"]), "url": r["url"], "image": r["image_url"],
              "age": r["age_limit"], "lat": r["lat"], "lon": r["lon"],
              "description": _clean(r["description"]),
              "last_days": bool(r["is_temporary"] and r["ends_on"] and (r["ends_on"] - start_day).days <= 7)}
        if lat is not None and lon is not None and r["lat"] is not None:
            ev["distance_m"] = round(travel.haversine_m(lat, lon, r["lat"], r["lon"]))
            ev["walk_min"] = travel.walk_min(lat, lon, r["lat"], r["lon"])
        seen[k] = ev
        by_day[day].append(ev)
    return {"from": start_day.isoformat(), "now": at.isoformat(), "group": group,
            "days": [{"date": d.isoformat(), "weekday": d.weekday(), "events": evs} for d, evs in sorted(by_day.items())],
            "groups": [{"id": g, "n": counts.get(g, 0)} for g in GROUPS if counts.get(g) or g == group]}
