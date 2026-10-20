"""2ГИС Places API (catalog.api.2gis.com/3.0): what OpenStreetMap lacks in Astana — opening hours, rating and
number of reviews, rubrics, Wi‑Fi, sockets, a step-free entrance, phones — and the places OSM has not mapped.

    python -m qydyr_data dgis --limit 500       # ask, store in dgis_items, merge into places

Two ways of asking, both cached on disk (data/cache/dgis) and remembered in dgis_lookups, so a run continues
where the last one stopped and the key's quota goes to new questions:
  - enrich: each OSM place by its name around its point -> the same place in 2ГИС (name and distance agree);
  - discover: typical rubrics ("кофейня", "детский центр", ...) around every populated ~1 km cell -> places
    OSM does not have.
Matches live in dgis_items and are merged into places on every `places` run: an OSM refresh never wipes them.
OSM keeps the last word where it has a value; 2ГИС fills the gaps and says so in attrs (…:source).
Data © 2ГИС, shown with attribution — check the API licence terms before a public launch."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from rapidfuzz import fuzz

from .. import hours, settings
from ..assistant.privacy import cell_center, geohash
from ..textnorm import name_keys
from .normalize import Place

log = logging.getLogger(__name__)

API_URL = "https://catalog.api.2gis.com/3.0/items"
FIELDS = ",".join(f"items.{f}" for f in ("point", "schedule", "reviews", "rubrics", "attribute_groups",
                                         "contact_groups", "name_ex", "address", "org"))
SOURCE_ID = "2gis"
ATTRIBUTION = "© 2ГИС"
DAYS = [("Mon", "Mo"), ("Tue", "Tu"), ("Wed", "We"), ("Thu", "Th"), ("Fri", "Fr"), ("Sat", "Sa"), ("Sun", "Su")]
ENRICH_CATEGORIES = ("culture", "food", "kids", "nightlife", "entertainment", "shopping", "work", "sport", "landmark")
MATCH_M, MATCH_NAME = 150, 80          # the same place: within 150 m and names that agree
DISCOVER = ["кофейня", "кафе", "ресторан", "детский развлекательный центр", "коворкинг", "бар", "музей",
            "кинотеатр", "торговый центр"]

# rubric (as 2ГИС names it) -> our taxonomy, for places OSM does not have; the first that fits wins
RUBRICS: list[tuple[str, tuple[str, str, str]]] = [
    (r"кофейн|кофе с собой", ("food", "cafe", "indoor")),
    (r"коворкинг", ("work", "coworking", "indoor")),
    (r"детск\w* (?:развлекательн|игров)|игровые комнаты|батут", ("kids", "kids_centre", "indoor")),
    (r"музе", ("culture", "museum", "indoor")),
    (r"театр", ("culture", "theatre", "indoor")),
    (r"галере|выставочн", ("culture", "gallery", "indoor")),
    (r"библиотек", ("culture", "library", "indoor")),
    (r"кинотеатр", ("entertainment", "cinema", "indoor")),
    (r"боулинг", ("entertainment", "bowling", "indoor")),
    (r"квест", ("entertainment", "escape_game", "indoor")),
    (r"караоке", ("nightlife", "karaoke", "indoor")),
    (r"кальян", ("nightlife", "hookah", "indoor")),
    (r"ночн\w* клуб", ("nightlife", "nightclub", "indoor")),
    (r"\bбар|паб", ("nightlife", "bar", "indoor")),
    (r"быстрое питание|фастфуд|фаст-фуд|шаурм|бургер", ("food", "fast_food", "indoor")),
    (r"ресторан", ("food", "restaurant", "indoor")),
    (r"кафе|кондитерск|пекарн|столов", ("food", "cafe", "indoor")),
    (r"торгово-развлекательн|торговые центры", ("shopping", "mall", "indoor")),
    (r"парк", ("park", "park", "outdoor")),
]
_RUBRIC_RE = [(re.compile(p, re.I), c) for p, c in RUBRICS]
WIFI_RE = re.compile(r"wi-?fi|вай-?фай|беспроводн\w* интернет", re.I)
SOCKET_RE = re.compile(r"розетк", re.I)
STEP_FREE_RE = re.compile(r"пандус|без ступен|для (?:людей|посетителей) с инвалидност|маломобильн|"
                          r"доступн\w* для (?:инвалид|колясоч)|вход для колясок|колясочник", re.I)


# ------------------------------------------------------------------ reading an item
def schedule_to_oh(schedule: dict | None) -> str | None:
    """2ГИС schedule {"Mon": {"working_hours": [{"from": "10:00", "to": "22:00"}]}, ..., "is_24x7": true}
    -> OSM opening_hours ("Mo-Fr 10:00-22:00; Sa,Su 11:00-23:00"), which the rest of the system already reads.
    A day missing from the schedule is a day off."""
    if not schedule:
        return None
    if schedule.get("is_24x7"):
        return "24/7"
    per_day = []
    for key, osm in DAYS:
        spans = []
        for wh in ((schedule.get(key) or {}).get("working_hours") or []):
            a, b = wh.get("from"), wh.get("to")
            if not (a and b):
                continue
            b = "24:00" if b == "00:00" else b
            spans.append(f"{a}-{b}")
        per_day.append((osm, ",".join(spans)))
    if not any(s for _, s in per_day):
        return None
    groups: list[tuple[list[str], str]] = []
    for osm, spans in per_day:
        if groups and groups[-1][1] == spans:
            groups[-1][0].append(osm)
        else:
            groups.append(([osm], spans))
    parts = []
    for days, spans in groups:
        if not spans:
            continue
        label = days[0] if len(days) == 1 else f"{days[0]},{days[1]}" if len(days) == 2 else f"{days[0]}-{days[-1]}"
        parts.append(f"{label} {spans}")
    oh = "; ".join(parts)
    return oh if hours.is_valid(oh) else None


def attribute_names(item: dict) -> list[str]:
    out = []
    for g in item.get("attribute_groups") or []:
        out.append(g.get("name") or "")
        out += [a.get("name") or "" for a in g.get("attributes") or []]
    return [x for x in out if x]


def rubric_names(item: dict) -> list[str]:
    return [r.get("name") for r in item.get("rubrics") or [] if r.get("name")]


def primary_name(item: dict) -> str:
    ex = item.get("name_ex") or {}
    return (ex.get("primary") or (item.get("name") or "").split(",")[0]).strip()


def contacts(item: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for g in item.get("contact_groups") or []:
        for c in g.get("contacts") or []:
            kind = c.get("type")
            if kind == "phone" and "phone" not in out:
                out["phone"] = c.get("value") or c.get("text")
            elif kind == "website" and "website" not in out:
                out["website"] = c.get("url") or c.get("value")
    return {k: v for k, v in out.items() if v}


def firm_url(item_id: str) -> str:
    return f"https://2gis.kz/astana/firm/{item_id.split('_')[0]}"


def category_for(item: dict) -> tuple[str, str, str] | None:
    for name in rubric_names(item):
        for rx, cat in _RUBRIC_RE:
            if rx.search(name):
                return cat
    return None


def same_place(place: Place | dict, item: dict) -> float:
    """How sure we are that an OSM place and a 2ГИС item are one place: 0 (no) .. 100."""
    pt = item.get("point") or {}
    if "lat" not in pt:
        return 0.0
    from ..assistant.travel import haversine_m
    lat, lon = (place.lat, place.lon) if isinstance(place, Place) else (place["lat"], place["lon"])
    d = haversine_m(lat, lon, pt["lat"], pt["lon"])
    if d > MATCH_M * 2:
        return 0.0
    names = [place.name, place.name_ru, place.name_kk, place.name_en, *place.aliases] if isinstance(place, Place) \
        else [place.get(k) for k in ("name", "name_ru", "name_kk", "name_en")]
    theirs = primary_name(item)
    sim = max((fuzz.token_set_ratio(n.lower(), theirs.lower()) for n in names if n), default=0)
    if name_keys(theirs) & set().union(*(name_keys(n) for n in names if n)):
        sim = max(sim, 95)
    return sim if d <= MATCH_M or sim >= 95 else 0.0


# ------------------------------------------------------------------ the API
class DgisClient:
    def __init__(self, key: str | None = None, *, cache_dir: Path | None = None, ttl_days: float = 7,
                 delay_s: float = 0.25, client: httpx.Client | None = None) -> None:
        self.key = key or settings.DGIS_API_KEY
        if not self.key:
            raise RuntimeError("QYDYR_DGIS_KEY is not set")
        self.cache_dir = cache_dir or settings.DATA_DIR / "cache" / "dgis"
        self.ttl_s = ttl_days * 86400
        self.delay_s = delay_s
        self.client = client or httpx.Client(timeout=15, headers={"User-Agent": settings.USER_AGENT})
        self.requests = 0          # real network requests in this run (the quota), cache hits not counted
        self._last = 0.0

    def search(self, q: str, lon: float, lat: float, radius: int, page: int = 1, page_size: int = 10) -> list[dict]:
        params = {"q": q, "point": f"{lon:.6f},{lat:.6f}", "radius": radius, "page": page,
                  "page_size": page_size, "fields": FIELDS, "locale": "ru_KZ"}
        cache = self.cache_dir / (hashlib.sha1(json.dumps(params, sort_keys=True, ensure_ascii=False).encode()).hexdigest() + ".json")
        if cache.exists() and time.time() - cache.stat().st_mtime < self.ttl_s:
            data = json.loads(cache.read_text(encoding="utf-8"))
        else:
            wait = self.delay_s - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            r = self.client.get(API_URL, params={**params, "key": self.key})
            self._last = time.monotonic()
            self.requests += 1
            data = r.json()
            code = (data.get("meta") or {}).get("code", r.status_code)
            if code not in (200, 404):   # 404 is 2ГИС's "nothing found"
                err = (data.get("meta") or {}).get("error") or {}
                raise RuntimeError(f"2ГИС API {code}: {err.get('type')} {err.get('message', '')}".strip())
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return ((data.get("result") or {}).get("items")) or []


# ------------------------------------------------------------------ asking
def _asked(conn, key: str, fresh_days: int) -> bool:
    row = conn.execute("select asked_at from dgis_lookups where key = %s", (key,)).fetchone()
    return bool(row) and row["asked_at"] > datetime.now(timezone.utc) - timedelta(days=fresh_days)


def _remember(conn, key: str, found: int) -> None:
    conn.execute("""insert into dgis_lookups (key, asked_at, found) values (%s, now(), %s)
                    on conflict (key) do update set asked_at = now(), found = excluded.found""", (key, found))


def _save(conn, item: dict, place_id: str | None, query: str) -> None:
    pt = item["point"]
    conn.execute(
        """insert into dgis_items (id, place_id, name, lat, lon, data, query, fetched_at)
           values (%s, %s, %s, %s, %s, %s::jsonb, %s, now())
           on conflict (id) do update set place_id = coalesce(excluded.place_id, dgis_items.place_id),
             name = excluded.name, lat = excluded.lat, lon = excluded.lon, data = excluded.data,
             query = excluded.query, fetched_at = now()""",
        (item["id"], place_id, primary_name(item), pt["lat"], pt["lon"], json.dumps(item, ensure_ascii=False), query))


def enrich(conn, client: DgisClient, limit: int, fresh_days: int = 30) -> dict:
    """Each OSM place (the categories people go to) by its name around its point."""
    rows = conn.execute(
        """select id, name, name_ru, name_kk, name_en, category, st_y(geom::geometry) lat, st_x(geom::geometry) lon
           from places where source_id = 'osm' and category = any(%s) and name <> 'Детская площадка'
           order by array_position(%s::text[], category), (hours_valid is false) desc, id""",
        (list(ENRICH_CATEGORIES), list(ENRICH_CATEGORIES))).fetchall()
    found = asked = 0
    for p in rows:
        if client.requests >= limit:
            break
        key = f"place:{p['id']}"
        if _asked(conn, key, fresh_days):
            continue
        items = client.search(p["name_ru"] or p["name"], p["lon"], p["lat"], radius=MATCH_M * 2, page_size=5)
        asked += 1
        best = max(items, key=lambda it: same_place(p, it), default=None)
        if best is not None and same_place(p, best) >= MATCH_NAME:
            _save(conn, best, p["id"], key)
            found += 1
        _remember(conn, key, int(best is not None and same_place(p, best) >= MATCH_NAME))
        conn.commit()
    return {"asked": asked, "matched": found}


def discover(conn, client: DgisClient, limit: int, queries: list[str] | None = None, fresh_days: int = 30,
             min_places: int = 3) -> dict:
    """Typical rubrics around every populated cell: places OSM has not mapped."""
    rows = conn.execute("""select category, st_y(geom::geometry) lat, st_x(geom::geometry) lon from places
                           where category <> 'anchor'""").fetchall()
    cells: dict[str, int] = {}
    for r in rows:
        c = geohash(r["lat"], r["lon"], 6)
        cells[c] = cells.get(c, 0) + 1
    busiest = sorted((c for c, n in cells.items() if n >= min_places), key=lambda c: -cells[c])
    new = asked = 0
    for cell in busiest:
        for q in queries or DISCOVER:
            if client.requests >= limit:
                return {"asked": asked, "new": new}
            key = f"area:{cell}:{q}"
            if _asked(conn, key, fresh_days):
                continue
            lat, lon = cell_center(cell)
            items = client.search(q, lon, lat, radius=700, page_size=10)
            asked += 1
            for it in items:
                if it.get("type", "branch") == "branch" and (it.get("point") or {}).get("lat") is not None:
                    _save(conn, it, None, key)
                    new += 1
            _remember(conn, key, len(items))
            conn.commit()
    return {"asked": asked, "new": new}


# ------------------------------------------------------------------ merging into places
def merge(p: Place, item: dict) -> None:
    """What 2ГИС knows and OSM does not, written into the place with its source."""
    item_id = item["id"]
    p.attrs["2gis:id"] = item_id
    p.attrs["2gis:url"] = firm_url(item_id)
    rv = item.get("reviews") or {}
    if rv.get("general_rating") is not None and rv.get("general_review_count"):
        p.attrs["2gis:rating"] = round(float(rv["general_rating"]), 1)
        p.attrs["2gis:reviews"] = int(rv["general_review_count"])
    rub = rubric_names(item)
    if rub:
        p.attrs["2gis:rubrics"] = rub[:3]
    if not p.hours_valid and (oh := schedule_to_oh(item.get("schedule"))):
        p.opening_hours, p.hours_valid = oh, True
        p.attrs["opening_hours:source"] = p.attrs["2gis:url"]
    names = " ".join(attribute_names(item))
    if WIFI_RE.search(names) and not p.attrs.get("wifi"):
        p.attrs["wifi"] = True
        p.attrs.setdefault("internet_access", "wlan")
        p.attrs["wifi:source"] = SOURCE_ID
    if SOCKET_RE.search(names):
        p.attrs["socket"] = "yes"
    if STEP_FREE_RE.search(names) and "wheelchair" not in p.attrs:
        p.attrs["wheelchair"] = "yes"
        p.attrs["wheelchair:source"] = SOURCE_ID
    c = contacts(item)
    p.phone = p.phone or c.get("phone")
    p.website = p.website or c.get("website")
    if not p.address and item.get("address_name"):
        p.address = item["address_name"]


def to_place(item: dict, fetched_at: datetime | None = None) -> Place | None:
    """A place OSM does not have, from its rubric; None when the rubric is not something people go out to."""
    cat = category_for(item)
    pt = item.get("point") or {}
    if cat is None or "lat" not in pt:
        return None
    category, sub, indoor = cat
    p = Place(id=f"2gis:{item['id']}", name=primary_name(item), category=category, subcategory=sub,
              lat=float(pt["lat"]), lon=float(pt["lon"]), indoor=indoor, source_id=SOURCE_ID,
              source_url=firm_url(item["id"]), fetched_at=fetched_at or datetime.now(timezone.utc))
    merge(p, item)
    return p


def _grid_key(lat: float, lon: float) -> tuple[int, int]:
    return round(lat / 0.002), round(lon / 0.003)          # ~220 m cells


def apply(conn, places: list[Place]) -> tuple[int, int]:
    """Merge stored 2ГИС items into the places about to be written, and add the ones OSM does not have.
    Returns (enriched, added)."""
    try:
        rows = conn.execute("select id, place_id, data, fetched_at from dgis_items").fetchall()
    except Exception:   # no table yet (an old database before init-db)
        conn.rollback()
        return 0, 0
    if not rows:
        return 0, 0
    by_id = {p.id: p for p in places}
    grid: dict[tuple[int, int], list[Place]] = {}
    for p in places:
        if p.source_id == "osm":
            grid.setdefault(_grid_key(p.lat, p.lon), []).append(p)

    def near(item: dict) -> list[Place]:
        gy, gx = _grid_key(item["point"]["lat"], item["point"]["lon"])
        return [p for dy in (-1, 0, 1) for dx in (-1, 0, 1) for p in grid.get((gy + dy, gx + dx), [])]

    enriched = added = 0
    linked = {r["place_id"] for r in rows if r["place_id"]}
    for r in rows:
        if r["place_id"] and r["place_id"] in by_id:
            merge(by_id[r["place_id"]], r["data"])
            enriched += 1
    for r in rows:
        item = r["data"]
        if r["place_id"] or "lat" not in (item.get("point") or {}):
            continue
        # a discovered item may still be an OSM place: link it rather than add a twin
        twin = max(near(item), key=lambda p: same_place(p, item), default=None)
        if twin is not None and same_place(twin, item) >= MATCH_NAME:
            if twin.id not in linked:
                merge(twin, item)
                linked.add(twin.id)
                enriched += 1
            continue
        own = by_id.get(f"2gis:{item['id']}")
        if own is not None:   # added on an earlier run: refresh what 2ГИС says now
            merge(own, item)
            continue
        p = to_place(item, r["fetched_at"])
        if p is not None:
            places.append(p)
            by_id[p.id] = p
            added += 1
    return enriched, added
