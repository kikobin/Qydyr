"""Match a venue string from an afisha ("Нацмузей РК, пр. Тауельсиздик 54") to a place in our base.

Order: exact name/alias key -> exact address -> fuzzy name. A fuzzy match is accepted only when it is
clearly better than the runner-up: an unmatched venue is better than a wrong one.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz, process

from ..textnorm import basic, name_keys, phonetic, strip_generic, translit

STREET_WORDS = {
    "пр", "просп", "проспект", "ул", "улица", "бульвар", "б р", "шоссе", "пер", "переулок", "наб",
    "набережная", "мкр", "микрорайон", "көшесі", "даңғылы", "к", "avenue", "ave", "street", "st",
    "г", "город", "астана", "astana", "здание", "зд", "дом", "д",
}
CULTURE_KINDS = {"exhibition", "performance", "concert", "lecture", "workshop", "film", "kids", "standup"}
# A venue that is only a type of place ("Музей", "Кафе") names nothing in particular.
CATEGORY_WORDS = {
    "музей", "museum", "галерея", "gallery", "театр", "theatre", "theater", "кинотеатр", "cinema", "кафе",
    "cafe", "кофейня", "ресторан", "restaurant", "бар", "bar", "парк", "park", "сквер", "библиотека",
    "library", "трц", "тц", "клуб", "club", "площадь", "набережная", "дворец", "центр", "зал", "арена",
    "көрме", "мұражай", "саябақ",
}
CULTURE_CATEGORIES = {"culture", "entertainment", "landmark"}


def _dist_m(a: "PlaceLite", b: "PlaceLite") -> float:
    dy = (a.lat - b.lat) * 111_320
    dx = (a.lon - b.lon) * 111_320 * math.cos(math.radians(a.lat))
    return math.hypot(dx, dy)


@dataclass
class PlaceLite:
    id: str
    name: str
    lat: float
    lon: float
    category: str = "other"
    name_ru: str | None = None
    name_kk: str | None = None
    name_en: str | None = None
    aliases: list[str] = field(default_factory=list)
    address: str | None = None
    opening_hours: str | None = None
    subcategory: str | None = None


@dataclass
class Match:
    place: PlaceLite
    score: float
    method: str  # exact | address | fuzzy


def address_key(addr: str | None) -> str:
    """'пр. Тауельсиздик, 54' and 'проспект Тәуелсіздік, 54' -> 'tauelsizdik 54'."""
    b = basic(addr)
    if not b:
        return ""
    words = [w for w in b.split() if w not in STREET_WORDS]
    if not any(re.fullmatch(r"\d+[а-яa-z]?", w) for w in words):
        return ""  # an address without a house number is too vague
    return phonetic(translit(" ".join(words)))


def _house_differs(address: str | None, p: "PlaceLite") -> bool:
    """Both addresses known and the house numbers differ: a similar name is then another venue."""
    a, b = re.findall(r"\d+", address_key(address)), re.findall(r"\d+", address_key(p.address))
    return bool(a and b) and a[0] != b[0]


class VenueMatcher:
    def __init__(self, places: list[PlaceLite]) -> None:
        self.places = {p.id: p for p in places}
        self.key_to_ids: dict[str, set[str]] = {}
        self.addr_to_ids: dict[str, set[str]] = {}
        for p in places:
            for n in (p.name, p.name_ru, p.name_kk, p.name_en, *p.aliases):
                for k in name_keys(n):
                    if len(k) >= 3:
                        self.key_to_ids.setdefault(k, set()).add(p.id)
            if ak := address_key(p.address):
                self.addr_to_ids.setdefault(ak, set()).add(p.id)
        self.choices = list(self.key_to_ids)

    def _prefer(self, ids: set[str], kind: str | None, same_name: bool = False) -> str | None:
        if len(ids) == 1:
            return next(iter(ids))
        if kind in CULTURE_KINDS:
            culture = [i for i in ids if self.places[i].category in CULTURE_CATEGORIES]
            if len(culture) == 1:
                return culture[0]
        if not same_name:  # one address = a mall and all its tenants: that stays ambiguous
            return None
        # One spot under one name: a monument + the stop/station named after it ("Бәйтерек").
        # Branches of a chain across the city stay ambiguous.
        ps = [self.places[i] for i in ids]
        if all(_dist_m(a, b) < 700 for a in ps for b in ps):
            real = [p for p in ps if p.category != "anchor"]
            if len(real) == 1:
                return real[0].id
            # one building mapped twice (ЦКЗ «Казахстан» = the Astana Musical outline + the Қазақконцерт node)
            if real and all(_dist_m(a, b) < 100 for a in real for b in real):
                return min(real, key=lambda p: (p.address is None, p.id)).id
        return None  # ambiguous

    @staticmethod
    def _names_agree(venue: str | None, p: PlaceLite) -> bool:
        """Same address is not enough when the names disagree: "Дворец Жастар, пр. Республики 34" is not
        the café "Qazan Food" at "улица Республики, 34" in a suburb 19 km away, and a stand-up bar is not
        the flower shop in the same building. A venue that names nothing ("Кафе") leaves it to the address."""
        v = basic(venue)
        if not v or set(v.split()) <= CATEGORY_WORDS | STREET_WORDS:
            return True
        vk = phonetic(translit(strip_generic(v)))
        return any(fuzz.token_set_ratio(vk, phonetic(translit(strip_generic(basic(n))))) >= 60
                   for n in (p.name, p.name_ru, p.name_kk, p.name_en, *p.aliases) if n)

    def match(self, venue: str | None, address: str | None = None, kind: str | None = None) -> Match | None:
        keys = name_keys(venue)
        # 1. exact name / alias
        hits: set[str] = set()
        for k in keys:
            hits |= self.key_to_ids.get(k, set())
        if hits and (pid := self._prefer(hits, kind, same_name=True)):
            return Match(self.places[pid], 100.0, "exact")

        # 2. exact address (from the address field or from the venue string itself)
        for a in (address, venue):
            ak = address_key(a)
            if ak and (ids := self.addr_to_ids.get(ak)) and (pid := self._prefer(ids, kind)) \
                    and (a is venue or self._names_agree(venue, self.places[pid])):
                return Match(self.places[pid], 95.0, "address")

        # 3. fuzzy name — only for a venue string that actually names something
        if not venue or set(basic(venue).split()) <= CATEGORY_WORDS:
            return None
        best: dict[str, float] = {}
        typed = bool(set(basic(venue).split()) & CATEGORY_WORDS)  # "Дворец …", "Театр …", "… зал"
        for q in keys:
            if len(q) < 5 or (" " not in q and len(q) < 7):
                continue
            for choice, score, _ in process.extract(q, self.choices, scorer=fuzz.token_set_ratio,
                                                    limit=10, score_cutoff=86):
                # token_set_ratio loves subsets ("музей" ⊂ any museum); temper it with plain ratio
                adj = 0.6 * score + 0.4 * fuzz.ratio(q, choice)
                for pid in self.key_to_ids[choice]:
                    if (typed and self.places[pid].category == "anchor") or _house_differs(address, self.places[pid]):
                        continue  # "Дворец Жастар" is not the hotel "Жастар"; Бараева 2/2 is not Есенберлина 10
                    best[pid] = max(best.get(pid, 0.0), adj)
        if not best:
            return None
        ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
        top_id, top = ranked[0]
        runner = ranked[1][1] if len(ranked) > 1 else 0.0
        if top >= 82 and top - runner >= 5:
            return Match(self.places[top_id], round(top, 1), "fuzzy")
        if top >= 82 and kind in CULTURE_KINDS:
            close = {pid for pid, s in ranked if top - s < 5}
            if (pid := self._prefer(close, kind)):
                return Match(self.places[pid], round(top, 1), "fuzzy")
        return None
