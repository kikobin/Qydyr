"""Where is the person? GPS if shared, otherwise the place they named, in any spelling or case form:
"возле Mega Silk Way", "у Чупа-чупса", "в AITU", "у здания Экспо 2017", "у Национального музея",
"Бәйтерек жанындамын".

Matching: exact name/alias keys, then the same phrase with Russian/Kazakh case endings stripped
("Пирамиды" ~ "Пирамида", "Дворца мира" ~ "Дворец мира"), longest phrase first; then fuzzy spelling.
OpenStreetMap often has one place twice (a node and a building, "Mega Silk Way" and "Мега Силк Вей",
a monument and the LRT stop named after it): copies on one spot collapse into the main object.
"""
from __future__ import annotations

import re
from dataclasses import replace
from functools import lru_cache

from .. import settings
from ..events.venues import CATEGORY_WORDS, PlaceLite, VenueMatcher, _dist_m
from ..textnorm import basic, name_keys, phonetic, strip_generic, translit
from .intent import anchor_candidates
from .models import Anchor, Intent

# A single word that only says what kind of place it is (or when, or with whom) names nothing in particular,
# even if some café is called "Вечер".
GENERIC_WORDS = CATEGORY_WORDS | {
    "столовая", "буфет", "кофейня", "площадка", "аллея", "фонтан", "вокзал", "остановка", "здание", "магазин",
    "кофе", "coffee", "обед", "ужин", "завтрак", "вечер", "утро", "ночь", "день", "сегодня", "завтра", "суббота",
    "воскресенье", "понедельник", "вторник", "среда", "четверг", "пятница", "выходные", "дом", "работа", "друг",
    "подруга", "ребёнок", "дети", "семья", "мама", "папа", "жена", "муж", "город", "центр", "берег"}
_RU_ENDINGS = sorted("ого его ому ему ыми ими ией ами ями ый ий ой ая яя ое ее ые ие ую юю ым им ых их ом ем ей "
                     "ия ии ию ах ях ов ев ам ям ою ею а я ы и у ю е о ь й".split(), key=len, reverse=True)
_KK_ENDINGS = sorted("ының інің дың дің тың тің ның нің нда нде да де та те ға ге қа ке ы і".split(),
                     key=len, reverse=True)
_KK_LETTERS = re.compile("[әғқңөұүһі]")
_PLAIN = {"railway_station", "bus_station", "hotel"}          # stops and hotels named after the landmark
_MINOR = {"artwork", "fountain", "memorial", "playground"}


# What people call a place for short, in any case form. "Мега" alone is Mega Silk Way, not the bar named
# "Mega" across the river (its stem "мег" is too short to match, and an alias would grab Mega Garden and Mega Arena).
COLLOQUIAL = [
    (re.compile(r"\b(?:мег(?:а|и|е|у|ой|ою)|mega)\b(?!\s*(?:silk|силк|garden|гарден|arena|арен|ice|айс))", re.I),
     "Mega Silk Way"),
]


def colloquial(text: str) -> str:
    for pattern, name in COLLOQUIAL:
        text = pattern.sub(name, text)
    return text


@lru_cache(maxsize=50_000)
def key_set(name: str | None) -> frozenset[str]:
    return frozenset(name_keys(name))


@lru_cache(maxsize=50_000)
def stem(word: str) -> str:
    """Crude case-ending strip for Cyrillic words: "Пирамиды" -> "пирамид", "Дворца" -> "дворц",
    "Шатырдың" -> "шатыр". Both the names and the request go through it, so it only has to be consistent."""
    w = word.lower()
    if not re.search("[а-яёәғқңөұүһі]", w):
        return w                                   # Latin names are compared as written
    for endings in ((_KK_ENDINGS,) if _KK_LETTERS.search(w) else ()) + (_RU_ENDINGS,):
        e = next((e for e in endings if w.endswith(e) and len(w) - len(e) >= 3), "")
        w = w[:len(w) - len(e)]
    w = re.sub(r"(?<=...)[аеёиоуыэюяйь]+$", "", w)    # музей / музея / музее -> муз
    return w[:-2] + "ц" if w.endswith("ец") else w   # fleeting vowel: дворец / дворца


@lru_cache(maxsize=50_000)
def stem_key(text: str) -> str:
    return phonetic(translit(" ".join(stem(w) for w in basic(text).split())))


def _tier(p: PlaceLite) -> int:
    """How likely a place is the one meant when several share a name: a stadium over an art object
    of the same name, anything over the LRT stop named after it."""
    if p.category == "anchor":
        return 0 if p.subcategory in _PLAIN else 1
    return 1 if p.subcategory in _MINOR or p.category in ("food", "nightlife", "kids", "work") else 2


def _size(p: PlaceLite) -> tuple:
    return p.id.split(":")[-1].startswith(("way", "relation")), len(p.aliases), p.address is not None


# ------------------------------------------------- curated aliases (config/place_overrides.yaml)
_RULES: tuple[float, list] | None = None


def _rules() -> list[tuple[str, list[str], set[str]]]:
    """The curated aliases, applied at query time too: a new alias works before the next places import."""
    global _RULES
    from ..places import overrides
    path = settings.CONFIG_DIR / "place_overrides.yaml"
    mtime = path.stat().st_mtime if path.exists() else 0.0
    if _RULES is None or _RULES[0] != mtime:
        rules = []
        for name, rule in (overrides.load(path) or {}).items():
            aliases = list((rule or {}).get("aliases", []))
            rules.append((name, aliases, set().union(*(key_set(a) for a in [name, *aliases]))))
        _RULES = (mtime, rules)
    return _RULES[1]


def _with_curated(places: list[PlaceLite]) -> list[PlaceLite]:
    """Same rule as places/overrides.py: a place takes the first rule that shares a name key with it."""
    by_key: dict[str, set[str]] = {}
    for p in places:
        for n in (p.name, p.name_ru, p.name_kk, p.name_en, *p.aliases):
            for k in key_set(n):
                by_key.setdefault(k, set()).add(p.id)
    extra: dict[str, list[str]] = {}
    for name, aliases, keys in _rules():
        for pid in set().union(*(by_key.get(k, set()) for k in keys)) - extra.keys():
            extra[pid] = [name, *aliases]
    return [replace(p, aliases=sorted({*p.aliases, *extra[p.id]} - {p.name})) if p.id in extra else p
            for p in places]


class Locator:
    _last: tuple | None = None

    @classmethod
    def of(cls, places: list[PlaceLite]) -> "Locator":
        """The API keeps one places list for 10 minutes: build the index once for it, not per request."""
        _rules()
        if cls._last is None or cls._last[0] is not places or cls._last[1] != _RULES[0]:  # type: ignore[index]
            cls._last = (places, _RULES[0], cls(places))  # type: ignore[index]
        return cls._last[2]

    def __init__(self, places: list[PlaceLite]) -> None:
        places = _with_curated(places)
        self.places = {p.id: p for p in places}
        self.matcher = VenueMatcher(places)
        self.exact: dict[str, set[str]] = {}
        self.strong: set[str] = set()                     # keys of whole names, not "Way" left of "Coffee Way"
        self.stems: dict[str, set[str]] = {}
        self.stems_weak: dict[str, set[str]] = {}
        for p in places:
            for n in (p.name, p.name_ru, p.name_kk, p.name_en, *p.aliases):
                b = basic(n)
                if not b:
                    continue
                for k in key_set(n):
                    self.exact.setdefault(k, set()).add(p.id)
                self.strong |= {b, translit(b), phonetic(translit(b))}
                self.stems.setdefault(stem_key(b), set()).add(p.id)
                if (g := strip_generic(b)) != b:
                    self.stems_weak.setdefault(stem_key(g), set()).add(p.id)
        self._generic = {stem_key(w) for w in GENERIC_WORDS}

    # ------------------------------------------------------------------ matching
    def _ids(self, phrase: str, loose: bool) -> set[str]:
        single = len(basic(phrase).split()) == 1
        if single and (basic(phrase) in GENERIC_WORDS or stem_key(phrase) in self._generic):
            return set()
        ids: set[str] = set()
        for k in key_set(phrase):
            if len(k) >= 3 and (not single or k in self.strong):
                ids |= self.exact.get(k, set())
        if ids or (single and not loose):
            return ids
        sk = stem_key(phrase)
        if len(sk.replace(" ", "")) < 4:
            return set()
        return set(self.stems.get(sk, set())) | (set() if single else self.stems_weak.get(sk, set()))

    def _choose(self, ids: set[str]) -> PlaceLite | None:
        """One place for a name, or None when the name is shared by places in different spots (a chain)."""
        ps = [self.places[i] for i in ids]
        top = max(_tier(p) for p in ps)
        ps = sorted((p for p in ps if _tier(p) == top), key=_size, reverse=True)
        return ps[0] if all(_dist_m(ps[0], p) < 700 for p in ps) else None

    def _scan(self, text: str, loose: bool = True, min_words: int = 1) -> PlaceLite | None:
        """Longest phrase first. A phrase that matched something claims its words: "Mega Silk Way" with
        two OSM copies never falls back to "Way" (a café called "Coffee Way" across the river)."""
        words = re.findall(r"[\w-]+", text)
        claimed = [False] * len(words)
        for n in range(min(5, len(words)), 0, -1):
            for i in range(len(words) - n + 1):
                phrase = " ".join(words[i:i + n])
                if any(claimed[i:i + n]) or len(basic(phrase).split()) < min_words \
                        or not (ids := self._ids(phrase, loose)):
                    continue
                claimed[i:i + n] = [True] * n
                if p := self._choose(ids):
                    return p
        return None

    def _fuzzy(self, text: str) -> PlaceLite | None:
        m = self.matcher.match(text)
        return m.place if m else None

    def _best(self, phrases: list[str]) -> PlaceLite | None:
        """A whole name ("Mega Silk Way") over a misspelt one ("Абу Даби Плаза") over one word of it ("Плаза")."""
        for f in (lambda c: self._scan(c, min_words=2), self._fuzzy, self._scan):
            if p := next((p for c in phrases if (p := f(c))), None):
                return p
        return None

    def find_place(self, text: str | None) -> PlaceLite | None:
        return self._best([text]) if text else None

    def resolve(self, intent: Intent, geocode=None) -> Anchor | None:
        """A place the person says they are at ("у Хан Шатыра", "кофе в Хан Шатыре", "Бәйтерек жанында")
        beats GPS: the bot sends the last shared location with every message, the words are newer.
        A destination ("хочу в Хан Шатыр") does not.
        geocode: an address ("я на Кенесары 40") -> {"lat", "lon", "label"} (places/dgis.Geocoder). It goes
        after a whole place name ("у Экспо 2017") and before a guess from one word of it (the Кенесары monument)."""
        gps = intent.lat is not None and intent.lon is not None
        text = colloquial(intent.text)
        cands = anchor_candidates(text)
        if intent.anchor_text and (said := colloquial(intent.anchor_text)) not in [c for c, _ in cands]:
            cands.append((said, True))   # Claude's reading of the request
        phrases = [c for c, here in sorted(cands, key=lambda c: not c[1]) if here or not gps]
        if geocode is not None and not any(self._scan(c, min_words=2) for c in phrases):
            if addr := next((a for c in phrases if (a := geocode(c))), None):
                return Anchor(lat=addr["lat"], lon=addr["lon"], label=addr["label"], source="address")
        place = self._best(phrases)
        if place is None and not gps:
            place = self._scan(text, loose=False) or self._fuzzy(text)
        if place:
            return Anchor(lat=place.lat, lon=place.lon, label=place.name, source="place", place_id=place.id)
        if gps:
            return Anchor(lat=intent.lat, lon=intent.lon, label="ваша геолокация", source="gps")
        return None

    def airport(self) -> PlaceLite | None:
        """The city airport (NQZ), not a skydiving airfield that OSM also tags as an aerodrome."""
        def rank(p: PlaceLite) -> tuple:
            names = " ".join(filter(None, [p.name, p.name_ru, p.name_en, *p.aliases])).lower()
            return ("nqz" in names, not re.search(r"парашют|parachute|аэроклуб|airfield", names),
                    bool(re.search(r"аэропорт|airport|әуежай", names)), len(p.aliases))
        return max((p for p in self.places.values() if p.subcategory == "airport"), key=rank, default=None)
