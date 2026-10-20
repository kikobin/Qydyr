"""Address -> coordinates for events whose venue is not in our places (Nominatim, OSM data).

Nominatim is an API with its own usage policy (not a site we crawl): at most 1 request per second,
an identifying User-Agent, results cached. A few dozen afisha addresses per run is well within it.
Only a hit with the same house number inside the Astana bbox counts: a wrong pin is worse than none.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

import httpx

from .. import settings

log = logging.getLogger(__name__)

URL = "https://nominatim.openstreetmap.org/search"
_PREFIX_RE = re.compile(r"^(ул|улица|пр-т|пр|просп|проспект|шоссе|ш|мкр|микрорайон|б-р|бульвар|наб|пер|к)(\.\s*|\s+)",
                        re.I)
_NOISE_RE = re.compile(r"\([^)]*\)|\b(блок|офис|оф|каб|этаж|ЖК|БЦ|ТРЦ|ТЦ|НП-?\d+)\b.*$", re.I)
_HOUSE = r"\d+(?:-?[а-яa-z](?![а-яa-z]))?(?:/\d+)?"
_HOUSE_RE = re.compile(rf"^\s*({_HOUSE})", re.I)
_STREET_HOUSE_RE = re.compile(rf"^(\D*[^\d\s.,])\.?\s+({_HOUSE})(?:\s|$)", re.I)  # "Габдуллина 18", "Куанышбаева 8/1"


def _house(s: str | None) -> str | None:
    m = _HOUSE_RE.match(s or "")
    return m.group(1).lower().replace("-", "") if m else None


def queries(address: str) -> list[tuple[str, str]]:
    """'пр. Мангилик Ел, 55 B2.4, Астана' -> [('Мангилик Ел, 55, Астана', '55')]. Needs street + house."""
    parts = [p.strip() for p in _NOISE_RE.sub("", address.replace("​", "")).split(",") if p.strip()]
    parts = [p for p in parts if p.lower() not in ("астана", "г. астана", "astana", "нур-султан")]
    for i, p in enumerate(parts):
        street = _PREFIX_RE.sub("", p)
        if i + 1 < len(parts) and (house := _house(parts[i + 1])) and not re.search(r"\d", p):
            return [(f"{street}, {house}, Астана", house)]
        if m := _STREET_HOUSE_RE.match(street):  # street and house in one part, no comma between
            house = m[2].lower().replace("-", "")
            return [(f"{m[1]}, {house}, Астана", house)]
    return []


class Geocoder:
    def __init__(self, cache_path: Path | None = None, delay_s: float = 1.1) -> None:
        self.cache_path = cache_path or settings.DATA_DIR / "cache" / "geocode.json"
        self.delay_s = delay_s
        self._last = 0.0
        try:
            self.cache: dict[str, list[float] | None] = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.cache = {}

    def save(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self.cache, ensure_ascii=False, indent=0), encoding="utf-8")

    def _search(self, q: str, house: str) -> list[float] | None:
        wait = self._last + self.delay_s - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        s, w, n, e = settings.ASTANA_BBOX
        r = httpx.get(URL, params={"q": q, "format": "jsonv2", "limit": 3, "addressdetails": 1,
                                   "viewbox": f"{w},{n},{e},{s}", "bounded": 1, "countrycodes": "kz"},
                      headers={"User-Agent": settings.USER_AGENT, "Accept-Language": "ru"}, timeout=20)
        self._last = time.monotonic()
        r.raise_for_status()
        for hit in r.json():
            if _house((hit.get("address") or {}).get("house_number")) == house:
                return [float(hit["lat"]), float(hit["lon"])]
        return None

    def locate(self, address: str | None) -> tuple[float, float] | None:
        if not address:
            return None
        for q, house in queries(address):
            if q not in self.cache:
                try:
                    self.cache[q] = self._search(q, house)
                except (httpx.HTTPError, ValueError) as e:
                    log.warning("geocode %r failed: %s", q, e)
                    continue
            if self.cache[q]:
                lat, lon = self.cache[q]
                return lat, lon
        return None
