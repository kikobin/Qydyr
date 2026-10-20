"""2ГИС Places API (catalog.api.2gis.com/3.0): what OpenStreetMap lacks in Astana — opening hours, rating and
number of reviews, rubrics, Wi‑Fi, sockets, a step-free entrance, phones — and the places OSM has not mapped.

    python -m qydyr_data dgis --limit 600       # ask, store in dgis_items, merge into places

Three ways of asking, all cached on disk (data/cache/dgis) and remembered in dgis_lookups, so a run continues
where the last one stopped and the key's quota goes to new questions:
  - rubrics (the main one): every branch of the rubrics people go out to, city-wide, page by page
    (RUBRIC_IMPORT: kids' play centres, museums, coworkings, coffee shops, ...). An item that is an OSM place
    (same name within 150 m) enriches it; the rest are places OSM does not have;
  - enrich: an OSM place still without a match, by its name around its point;
  - discover: free-text rubric queries around populated ~1 km cells (for rubrics we have no id for).
What the real API returns (fields, attribute tags) was checked with `python -m qydyr_data dgis-probe`.
Matches live in dgis_items and are merged into places on every `places` run: an OSM refresh never wipes them.
OSM keeps the last word where it has a value; 2ГИС fills the gaps and says so in attrs (…:source).
Data © 2ГИС, shown with attribution — check the API licence terms before a public launch."""
from __future__ import annotations

import gzip
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
from ..textnorm import basic, name_keys, phonetic, translit
from .normalize import Place

log = logging.getLogger(__name__)

API_URL = "https://catalog.api.2gis.com/3.0/items"
GEOCODE_URL = "https://catalog.api.2gis.com/3.0/items/geocode"
REGION_ID = 68                       # Астана in 2ГИС
PAGE_SIZE, MAX_PAGES = 10, 5         # the most the key allows: 50 results per query
# metres from a geohash cell centre to its corners in Astana (5: 3.1 x 4.9 km, 6: 770 x 610 m): the circle
# covers the cell and little more, so neighbouring cells do not fetch the same branches again
AREA_RADIUS = {5: 2900, 6: 500}
FIELDS = ",".join(f"items.{f}" for f in ("point", "schedule", "reviews", "rubrics", "attribute_groups",
                                         "contact_groups", "name_ex", "address", "org", "links", "flags", "context"))
# rubric id (Астана, from /2.0/catalog/rubric/search) -> our taxonomy; tiers: what to import first under a quota
RUBRIC_IMPORT: list[tuple[int, str, tuple[str, str, str]]] = [
    (1, "16743", ("kids", "kids_centre", "indoor")),         # Детские игровые залы, 73
    (1, "193", ("culture", "museum", "indoor")),             # Музеи
    (1, "190", ("culture", "gallery", "indoor")),            # Художественные выставки
    (1, "199", ("culture", "theatre", "indoor")),            # Театры
    (1, "200", ("culture", "concert_hall", "indoor")),       # Филармония
    (1, "51001", ("culture", "concert_hall", "indoor")),     # Концертные залы
    (1, "192", ("entertainment", "cinema", "indoor")),       # Кинотеатры
    (1, "19499", ("shopping", "mall", "indoor")),            # Торгово-развлекательные центры
    (1, "104110", ("work", "coworking", "indoor")),          # Коворкинг
    (1, "72370", ("food", "anticafe", "indoor")),            # Антикафе
    (1, "173", ("nightlife", "nightclub", "indoor")),        # Ночные клубы
    (1, "170", ("entertainment", "bowling", "indoor")),      # Боулинг
    (1, "110300", ("entertainment", "escape_game", "indoor")),  # Квесты
    (1, "112442", ("entertainment", "vr", "indoor")),        # Центры виртуальной реальности
    (1, "110427", ("kids", "trampoline_park", "indoor")),    # Батутные центры
    (1, "11974", ("sport", "ice_rink", "indoor")),           # Каток
    (1, "168", ("park", "park", "outdoor")),                 # Парки
    (1, "189", ("culture", "library", "indoor")),            # Библиотеки
    (1, "111526", ("landmark", "viewpoint", "mixed")),       # Смотровые площадки
    (1, "167", ("entertainment", "zoo", "outdoor")),         # Зоопарк
    (1, "15800", ("entertainment", "aquarium", "indoor")),   # Океанариум
    (1, "112609", ("nightlife", "karaoke", "indoor")),       # Караоке-боксы
    (1, "112658", ("food", "cafe", "indoor")),               # Кафе-кондитерские
    (2, "162", ("food", "cafe", "indoor")),                  # Кофейни, 927
    (2, "164", ("food", "restaurant", "indoor")),            # Рестораны, 510
    (2, "159", ("nightlife", "bar", "indoor")),              # Бары, 658
    (2, "21387", ("nightlife", "karaoke", "indoor")),        # Караоке-залы
    (3, "161", ("food", "cafe", "indoor")),                  # Кафе, 1355
    (3, "165", ("food", "fast_food", "indoor")),             # Быстрое питание, 1101
]
RUBRIC_TAXONOMY = {rid: cat for _, rid, cat in RUBRIC_IMPORT}
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
                          r"доступн\w* (?:для (?:инвалид|колясоч)|вход)|вход для колясок|колясочник|широкий лифт", re.I)
# attribute tags as 2ГИС returns them (seen in the probe): tag prefix -> what it tells us
TAG_ACCESSIBLE = "accessible_entrance"            # "Доступная среда": пандус, широкий лифт, автоматическая дверь
TAG_LAPTOP = "food_service_cafe_working"          # "Можно с ноутбуком"
TAG_KIDS_MENU = "food_service_details_food_kids_menu"
TAG_ENGLISH_MENU = "food_service_details_food_english_menu"
TAG_AVG_PRICE = "food_service_avg_price"          # "Чек 7000 ₸"
TAG_CAPACITY = "food_service_capacity"            # "До 45 мест"
TAG_PANORAMA = "food_service_infrastructure_panoramic_restaurant"   # "Панорамный вид"
TAG_LIVE_MUSIC = "food_service_details_food_live_music"
TAG_LUNCH = "food_service_business_lunch"         # "Бизнес-ланч"
TAG_DOG = "food_service_dog_friendly_food"        # "Можно с собакой"
TAG_OUTDOOR = ("food_service_infrastructure_patio", "food_service_infrastructure_outdoor_tables")
TAG_CUISINE = "food_service_food_"                # "Европейская кухня", "Восточная кухня"
KIDS_AREA_RE = re.compile(r"детск\w* (?:комнат|площадк|уголок|зон)|игров\w* (?:комнат|зон)", re.I)
OUTDOOR_RE = re.compile(r"летн\w* (?:веранд|терраса|площадк)|терраса|веранда", re.I)
PRICE_RE = re.compile(r"(\d[\d\s]*)\s*₸")
# an address as people type it: "Кенесары 40", "улица Сыганак 10/1", "пр. Мангилик Ел 55", "Абай даңғылы 12"
STREET_WORDS = (r"ул(?:ица|ице|ицу|\.)?|пр(?:оспект\w*|осп\.?|-т|\.)|шоссе|бульвар\w*|б-р|переул\w*|пер\.|"
                r"мкр\.?|микрорайон\w*|жк|көшесі|даңғылы|street|st\.|avenue|ave\.?")
_WORD = r"[^\W\d_][\w\-]*"
ADDRESS_RE = re.compile(rf"(?:(?:{STREET_WORDS})\s*)?({_WORD}(?:\s+{_WORD}){{0,2}}),?\s+(?:дом\s+|д\.\s*)?"
                        rf"(\d{{1,4}})(?!\d)(?:\s*[/\-]\s*\d{{1,3}}|[а-яa-z]\b)?"
                        r"(?![\d:.,]\d|\s*(?:ч\b|час|мин|сағ|этаж|эт\.|qabat|қабат|чел|адам|раз|км|м\b|лет|год|жыл|тенге|тг|₸|"
                        r"%|шт|дет|реб|взросл|h\b|hour|min|floor|people|kid))", re.I)
_STREET_ONLY = re.compile(rf"^(?:{STREET_WORDS})$", re.I)
_NOT_A_NAME = {"на", "в", "во", "до", "за", "через", "с", "со", "по", "около", "от", "к", "и", "или", "у", "возле",
               "рядом", "минут", "часа", "часов", "в течение", "kids", "for", "at", "in", "on"}


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


def attributes(item: dict) -> list[dict]:
    """[{"group": "Доступная среда", "name": "Пандус", "tag": "accessible_entrance_..."}, ...]"""
    return [{"group": g.get("name") or "", "name": a.get("name") or "", "tag": a.get("tag") or ""}
            for g in item.get("attribute_groups") or [] for a in g.get("attributes") or []]


def attribute_names(item: dict) -> list[str]:
    out = []
    for g in item.get("attribute_groups") or []:
        out.append(g.get("name") or "")
        out += [a.get("name") or "" for a in g.get("attributes") or []]
    return [x for x in out if x]


def rubric_names(item: dict) -> list[str]:
    return [r.get("name") for r in item.get("rubrics") or [] if r.get("name")]


def primary_name(item: dict) -> str:
    """"Coffee Boom" from "Coffee Boom, кофейня"; an unnamed attraction (a viewpoint) by what it is."""
    ex = item.get("name_ex") or {}
    name = (ex.get("primary") or (item.get("name") or "").split(",")[0]).strip()
    return name or (item.get("full_name") or "").removeprefix("Астана, ").split(",")[0].strip()


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

    def get(self, params: dict, url: str = API_URL, retries: int = 4) -> dict:
        """One API call, from the disk cache when fresh. The server drops connections when asked fast:
        wait and retry instead of failing the whole run."""
        cache = self.cache_dir / (hashlib.sha1(json.dumps([url, params], sort_keys=True, ensure_ascii=False).encode()).hexdigest() + ".json")
        if cache.exists() and time.time() - cache.stat().st_mtime < self.ttl_s:
            return json.loads(cache.read_text(encoding="utf-8"))
        for attempt in range(retries):
            wait = self.delay_s - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            try:
                r = self.client.get(url, params={**params, "key": self.key})
                self._last = time.monotonic()
                self.requests += 1
                if r.status_code in (429, 502, 503, 504):
                    raise httpx.TransportError(f"HTTP {r.status_code}")
                data = r.json()
                break
            except (httpx.TransportError, ValueError) as e:
                self._last = time.monotonic()
                if attempt == retries - 1:
                    raise RuntimeError(f"2ГИС unreachable: {e}") from e
                time.sleep(2 ** attempt * 1.5)
        code = (data.get("meta") or {}).get("code", r.status_code)
        if code not in (200, 404):   # 404 is 2ГИС's "nothing found"
            err = (data.get("meta") or {}).get("error") or {}
            raise RuntimeError(f"2ГИС API {code}: {err.get('type')} {err.get('message', '')}".strip())
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data

    def search(self, q: str, lon: float, lat: float, radius: int, page: int = 1, page_size: int = PAGE_SIZE) -> list[dict]:
        data = self.get({"q": q, "point": f"{lon:.6f},{lat:.6f}", "radius": radius, "page": page,
                         "page_size": page_size, "fields": FIELDS, "locale": "ru_KZ"})
        return ((data.get("result") or {}).get("items")) or []

    def rubric_page(self, rubric_id: str, page: int, cell: str | None = None) -> tuple[list[dict], int]:
        """One page of a rubric's branches, city-wide or around a geohash cell: (items, total)."""
        params = {"rubric_id": rubric_id, "region_id": REGION_ID, "page": page, "page_size": PAGE_SIZE,
                  "fields": FIELDS, "locale": "ru_KZ"}
        if cell:
            lat, lon = cell_center(cell)
            params.update(point=f"{lon:.6f},{lat:.6f}", radius=AREA_RADIUS[len(cell)])
        data = self.get(params)
        res = data.get("result") or {}
        return res.get("items") or [], int(res.get("total") or 0)


# ------------------------------------------------------------------ addresses
def address_part(text: str) -> str | None:
    """The address in a phrase ("Кенесары 40 с ребёнком" -> "Кенесары 40"), or None: a name with a year
    ("Экспо 2017") or a count ("2 часа") is not an address."""
    for m in ADDRESS_RE.finditer(text):
        words = m[1].split()
        if any(w.lower() in _NOT_A_NAME for w in words):     # "кофе на 2", "через 15": a count, not a house
            continue
        street_said = bool(re.match(rf"(?:{STREET_WORDS})\b", m[0], re.I)) or any(_STREET_ONLY.match(w) for w in words)
        if all(_STREET_ONLY.match(w) for w in words) or (1900 <= int(m[2]) <= 2100 and not street_said):
            continue
        return m[0].strip(" ,")
    return None


def geocode(client: "DgisClient", text: str) -> dict | None:
    """An address in Astana -> {"lat", "lon", "label"}, through the 2ГИС geocoder. The answer must name the
    street that was asked and the house number: the geocoder answers "Астана" to things it does not know."""
    addr = address_part(text)
    if not addr:
        return None
    m = ADDRESS_RE.search(addr)

    def latin(s: str) -> str:              # "Kenesary" = "Кенесары", "Бейбітшілік" = "Бейбитшилик"
        return phonetic(translit(basic(s)))
    street = latin(" ".join(w for w in m[1].split() if not _STREET_ONLY.match(w)))
    q = re.sub(r"даңғылы", "проспект", re.sub(r"көшесі", "улица", addr, flags=re.I), flags=re.I)  # it reads Russian
    data = client.get({"q": f"Астана, {q}", "fields": "items.point,items.full_address_name", "locale": "ru_KZ"},
                      url=GEOCODE_URL, retries=1)
    south, west, north, east = settings.ASTANA_BBOX
    for it in ((data.get("result") or {}).get("items") or []):
        pt = it.get("point") or {}
        found = it.get("address_name") or it.get("full_address_name") or ""
        if it.get("type") != "building" or not (south <= pt.get("lat", 0) <= north and west <= pt.get("lon", 0) <= east):
            continue
        if not (it.get("full_address_name") or "Астана").startswith("Астана"):   # a village by the city
            continue
        if any(re.search(said, q, re.I) and not re.search(kind, found, re.I)       # an avenue is not a street
               for said, kind in ((r"\bпр(?:оспект|осп|-т|\.)", "проспект"), (r"\bул(?:иц|\.)", "улица"))):
            continue
        # the street without its case ending ("Кенесары" ~ "Кенесары улица", "Сарайшыке" ~ "Сарайшык")
        if fuzz.partial_ratio(street[:max(4, len(street) - 2)], latin(found)) < 80 or m[2] not in re.findall(r"\d+", found):
            continue
        name = (it.get("name") or "").split(",")[0].strip()
        label = found.replace("Астана, ", "")
        if name and name.lower() not in found.lower():           # not "проспект Республики, 18" again
            label = f"{label} · {name}"               # "Кабанбай батыр проспект, 62 · MEGA Silk Way"
        return {"lat": float(pt["lat"]), "lon": float(pt["lon"]), "label": label}
    return None


class Geocoder:
    """Addresses people type ("я на Кенесары 40") for the assistant's start point. Only the address words go
    to 2ГИС, not who asked; answers are cached on disk; after a failure it rests for 5 minutes, so a 2ГИС
    outage costs one slow answer, not every answer."""
    def __init__(self, client: "DgisClient | None" = None) -> None:
        self._client = client
        self._rest_until = 0.0

    @classmethod
    def default(cls) -> "Geocoder | None":
        return cls() if settings.DGIS_API_KEY else None

    def __call__(self, text: str) -> dict | None:
        if not address_part(text) or time.time() < self._rest_until:
            return None
        try:
            self._client = self._client or DgisClient(delay_s=0, client=httpx.Client(
                timeout=4, headers={"User-Agent": settings.USER_AGENT}))
            return geocode(self._client, text)
        except Exception as e:
            log.warning("2ГИС geocoder: %s", e)
            self._rest_until = time.time() + 300
            return None


# ------------------------------------------------------------------ asking
def _asked(conn, key: str, fresh_days: int) -> bool:
    row = conn.execute("select asked_at from dgis_lookups where key = %s", (key,)).fetchone()
    return bool(row) and row["asked_at"] > datetime.now(timezone.utc) - timedelta(days=fresh_days)


def _remember(conn, key: str, found: int) -> None:
    conn.execute("""insert into dgis_lookups (key, asked_at, found) values (%s, now(), %s)
                    on conflict (key) do update set asked_at = now(), found = excluded.found""", (key, found))


_NOISE = {"rubric_ids", "icon_url", "is_context", "is_primary"}


def slim(item: dict) -> dict:
    """An item without what we never read: each attribute group lists the hundreds of rubric ids it applies to."""
    return {**item, "attribute_groups": [
        {**{k: v for k, v in g.items() if k not in _NOISE},
         "attributes": [{k: v for k, v in a.items() if k not in _NOISE} for a in g.get("attributes") or []]}
        for g in item.get("attribute_groups") or []]}


def _save(conn, item: dict, place_id: str | None, query: str) -> None:
    item = slim(item)
    pt = item["point"]
    conn.execute(
        """insert into dgis_items (id, place_id, name, lat, lon, data, query, fetched_at)
           values (%s, %s, %s, %s, %s, %s::jsonb, %s, now())
           on conflict (id) do update set place_id = coalesce(excluded.place_id, dgis_items.place_id),
             name = excluded.name, lat = excluded.lat, lon = excluded.lon, data = excluded.data,
             query = excluded.query, fetched_at = now()""",
        (item["id"], place_id, primary_name(item), pt["lat"], pt["lon"], json.dumps(item, ensure_ascii=False), query))


def _populated(conn, precision: int, min_places: int = 3, parent: str = "") -> list[str]:
    rows = conn.execute("""select st_y(geom::geometry) lat, st_x(geom::geometry) lon from places
                           where category <> 'anchor'""").fetchall()
    n: dict[str, int] = {}
    for r in rows:
        c = geohash(r["lat"], r["lon"], precision)
        if c.startswith(parent):
            n[c] = n.get(c, 0) + 1
    return sorted((c for c, k in n.items() if k >= min_places), key=lambda c: -n[c])


class _Quota(Exception):
    pass


def import_rubrics(conn, client: DgisClient, limit: int, tiers: tuple[int, ...] = (1, 2, 3),
                   fresh_days: int = 30) -> dict:
    """Every branch of the rubrics people go out to, the most useful tiers first. A query gives at most 50
    results: a rubric with more is asked district by district (~5 km), a dense district cell by cell (~1 km)."""
    got: dict[str, int] = {}

    def page(rid: str, cell: str | None, n: int) -> tuple[int, int]:
        """(items on the page, total) — from the lookups when asked already."""
        key = f"rubric:{rid}:{cell or 'city'}:{n}"
        row = conn.execute("select found from dgis_lookups where key = %s and asked_at > now() - %s * interval '1 day'",
                           (key, fresh_days)).fetchone()
        tot = conn.execute("select found from dgis_lookups where key = %s", (f"rubric:{rid}:{cell or 'city'}:total",)).fetchone()
        if row and tot:
            return row["found"], tot["found"]
        if client.requests >= limit:
            raise _Quota
        items, total = client.rubric_page(rid, n, cell)
        for it in items:
            if (it.get("point") or {}).get("lat") is not None:
                _save(conn, it, None, key)
        _remember(conn, key, len(items))
        _remember(conn, f"rubric:{rid}:{cell or 'city'}:total", total)
        conn.commit()
        got[rid] = got.get(rid, 0) + len(items)
        return len(items), total

    def area(rid: str, cell: str | None) -> None:
        k, total = page(rid, cell, 1)
        if total > PAGE_SIZE * MAX_PAGES and (cell is None or len(cell) < 6):
            for child in _populated(conn, 5 if cell is None else 6, parent=cell or ""):
                area(rid, child)
            return
        n = 1
        while k == PAGE_SIZE and n < MAX_PAGES:
            n += 1
            k, _ = page(rid, cell, n)

    try:
        for tier, rid, _ in RUBRIC_IMPORT:
            if tier in tiers:
                area(rid, None)
    except _Quota:
        pass
    return got


def enrich(conn, client: DgisClient, limit: int, fresh_days: int = 30) -> dict:
    """Each OSM place (the categories people go to) by its name around its point."""
    rows = conn.execute(
        """select id, name, name_ru, name_kk, name_en, category, st_y(geom::geometry) lat, st_x(geom::geometry) lon
           from places where source_id = 'osm' and category = any(%s) and name <> 'Детская площадка'
             and not attrs ? '2gis:id'          -- already matched through a rubric import
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


# ------------------------------------------------------------------ moving the answers between databases
EXPORT_FILE = settings.DATA_DIR / "dgis_items.jsonl.gz"


def export_items(conn, path: Path | None = None) -> int:
    """Everything asked so far, to one file: another database (production) gets it without spending the key.
    The questions go with the answers (dgis_lookups), so a run there does not ask them again."""
    path = path or EXPORT_FILE
    rows = conn.execute("select id, place_id, query, fetched_at, data from dgis_items order by id").fetchall()
    asked = conn.execute("select key, asked_at, found from dgis_lookups order by key").fetchall()
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps({"id": r["id"], "place_id": r["place_id"], "query": r["query"],
                                "fetched_at": r["fetched_at"].isoformat(), "data": slim(r["data"])},
                               ensure_ascii=False) + "\n")
        for r in asked:
            f.write(json.dumps({"lookup": r["key"], "asked_at": r["asked_at"].isoformat(), "found": r["found"]},
                               ensure_ascii=False) + "\n")
    return len(rows)


def import_items(conn, path: Path | None = None) -> int:
    n = 0
    with gzip.open(path or EXPORT_FILE, "rt", encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if "lookup" in r:
                conn.execute("""insert into dgis_lookups (key, asked_at, found) values (%s, %s, %s)
                                on conflict (key) do update set asked_at = greatest(excluded.asked_at, dgis_lookups.asked_at),
                                  found = excluded.found""", (r["lookup"], r["asked_at"], r["found"]))
                continue
            pt = r["data"]["point"]
            conn.execute(
                """insert into dgis_items (id, place_id, name, lat, lon, data, query, fetched_at)
                   values (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                   on conflict (id) do update set place_id = coalesce(excluded.place_id, dgis_items.place_id),
                     data = excluded.data, fetched_at = greatest(excluded.fetched_at, dgis_items.fetched_at)""",
                (r["id"], r["place_id"], primary_name(r["data"]), pt["lat"], pt["lon"],
                 json.dumps(r["data"], ensure_ascii=False), r["query"], r["fetched_at"]))
            n += 1
    conn.commit()
    return n


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
    attrs_ = attributes(item)
    names = " ".join(a["name"] for a in attrs_)
    tags = {a["tag"] for a in attrs_}
    if (WIFI_RE.search(names) or any("wifi" in t for t in tags)) and not p.attrs.get("wifi"):
        p.attrs["wifi"] = True
        p.attrs.setdefault("internet_access", "wlan")
        p.attrs["wifi:source"] = SOURCE_ID
    if SOCKET_RE.search(names):
        p.attrs["socket"] = "yes"
    if TAG_LAPTOP in tags:                         # "Можно с ноутбуком": a table, a socket, nobody minds
        p.attrs["laptop"] = "yes"
    step_free = [a["name"] for a in attrs_ if a["tag"].startswith(TAG_ACCESSIBLE) or STEP_FREE_RE.search(a["name"])]
    if step_free and "wheelchair" not in p.attrs:
        p.attrs["wheelchair"] = "yes"
        p.attrs["wheelchair:source"] = SOURCE_ID
    if step_free:
        p.attrs["access:features"] = sorted(set(step_free))[:4]
    if TAG_KIDS_MENU in tags or KIDS_AREA_RE.search(names):
        p.attrs["kids_friendly"] = "kids_menu" if TAG_KIDS_MENU in tags and not KIDS_AREA_RE.search(names) else "kids_area"
    if TAG_ENGLISH_MENU in tags:
        p.attrs["english_menu"] = "yes"
    if (OUTDOOR_RE.search(names) or any(t.startswith(TAG_OUTDOOR) for t in tags)) and "outdoor_seating" not in p.attrs:
        p.attrs["outdoor_seating"] = "yes"
    if TAG_PANORAMA in tags and "view" not in p.attrs:
        p.attrs["view"], p.attrs["view:source"] = "yes", SOURCE_ID
    if TAG_DOG in tags and "dog" not in p.attrs:                 # OSM tags it the same way: dog=yes
        p.attrs["dog"], p.attrs["dog:source"] = "yes", SOURCE_ID
    if TAG_LIVE_MUSIC in tags:
        p.attrs["live_music"] = "yes"
    if TAG_LUNCH in tags:
        p.attrs["business_lunch"] = "yes"
    if cuisine := [a["name"] for a in attrs_ if a["tag"].startswith(TAG_CUISINE) and a["name"].lower().endswith("кухня")]:
        p.attrs["2gis:cuisine"] = cuisine[:3]
    for a in attrs_:
        if a["tag"] == TAG_AVG_PRICE and (m := PRICE_RE.search(a["name"])):
            p.attrs["avg_check"] = int(re.sub(r"\s", "", m[1]))
        elif a["tag"] == TAG_CAPACITY:
            p.attrs["capacity_text"] = a["name"]
    addr = item.get("address") or {}
    building = addr.get("building_name") or item.get("building_name")
    if building and primary_name(item).lower() not in building.lower():
        p.attrs["in_building"] = building            # "ТРЦ Керуен"
        p.indoor = "indoor"
    if item.get("address_comment"):
        p.attrs["where_inside"] = item["address_comment"]   # "3 этаж", "вход со двора"
    if (sc := (item.get("schedule") or {}).get("comment")):
        p.attrs["hours_note"] = sc                   # "касса до 20:30; технический перерыв 13:00–13:30"
    st = [s for s in (item.get("links") or {}).get("nearest_stations") or [] if s.get("distance") is not None]
    if st:
        s0 = min(st, key=lambda s: s["distance"])
        p.attrs["nearest_station"] = {"name": s0.get("name"), "m": int(s0["distance"]),
                                      "types": s0.get("route_types") or []}
    badges = [b.get("text") for b in (item.get("flags") or {}).get("badges") or [] if b.get("text")]
    if badges:
        p.attrs["2gis:badges"] = badges[:2]
    c = contacts(item)
    p.phone = p.phone or c.get("phone")
    p.website = p.website or c.get("website")
    if not p.address and item.get("address_name"):
        p.address = item["address_name"]


def to_place(item: dict, fetched_at: datetime | None = None) -> Place | None:
    """A place OSM does not have, from its rubric; None when the rubric is not something people go out to."""
    primary = [r for r in item.get("rubrics") or [] if r.get("kind") == "primary"] + list(item.get("rubrics") or [])
    cat = next((RUBRIC_TAXONOMY[str(r.get("id"))] for r in primary if str(r.get("id")) in RUBRIC_TAXONOMY), None) \
        or category_for(item)
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
