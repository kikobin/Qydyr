"""What the assistant knows about a place or event, and how sure it is.

Every match is tri-state with evidence: yes (data says so), likely (typical for this kind of place),
unknown, no. "Розетки: вероятно — кафе с Wi‑Fi" is honest; "есть розетки" would be invented.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from .. import settings

WEIGHT = {"yes": 1.0, "likely": 0.6, "unknown": 0.25, "no": 0.0}

VISIT: dict[str, tuple[int, int]] = {
    "museum": (60, 90), "gallery": (40, 60), "theatre": (120, 150), "cinema": (110, 140), "planetarium": (60, 75),
    "arts_centre": (45, 90), "library": (45, 90), "community_centre": (45, 90),
    "cafe": (30, 45), "restaurant": (60, 90), "fast_food": (20, 30), "food_court": (25, 40), "ice_cream": (10, 20),
    "bar": (60, 90), "pub": (60, 90), "nightclub": (120, 180), "coworking": (60, 120),
    "park": (30, 60), "garden": (30, 45), "playground": (30, 45), "viewpoint": (15, 25), "attraction": (30, 45),
    "artwork": (5, 10), "fountain": (10, 15), "memorial": (10, 15), "monument": (10, 15),
    "mall": (45, 90), "marketplace": (30, 60), "zoo": (90, 120), "aquarium": (60, 90), "theme_park": (90, 150),
    "water_park": (120, 180), "ice_rink": (60, 90), "escape_game": (60, 75), "bowling": (60, 90),
    "trampoline_park": (60, 90), "arcade": (45, 60), "minigolf": (45, 60), "sports_centre": (60, 90),
    "concert_hall": (90, 120), "events_venue": (60, 120), "exhibition_centre": (45, 90),
    "place_of_worship": (15, 30), "hookah": (60, 120), "karaoke": (90, 150), "biergarten": (60, 90),
    "stadium": (90, 150), "dance": (60, 90), "books": (20, 40), "souvenir": (15, 30),
}
EVENT_VISIT = {"exhibition": (40, 60), "concert": (90, 120), "performance": (120, 150), "festival": (60, 120),
               "fair": (45, 90), "workshop": (90, 120), "lecture": (60, 90), "party": (120, 180),
               "excursion": (60, 90), "sport": (90, 120), "kids": (60, 90), "film": (100, 130), "standup": (90, 120),
               "other": (60, 90)}

IDEAS = {
    "museum": "начните с главного зала, потом — временная экспозиция",
    "gallery": "посмотрите текущую выставку целиком, это не займёт много времени",
    "theatre": "проверьте в кассе спектакли на сегодня",
    "cinema": "посмотрите ближайшие сеансы на месте",
    "planetarium": "попадите на ближайший сеанс в куполе",
    "library": "найдите тихий читальный зал",
    "cafe": "возьмите горячий напиток и займите место у окна",
    "restaurant": "закажите что-нибудь из национальной кухни",
    "fast_food": "быстро перекусите",
    "food_court": "выберите кухню по настроению",
    "bar": "спросите фирменный напиток",
    "pub": "спросите фирменный напиток",
    "nightclub": "уточните программу вечера на входе",
    "coworking": "возьмите рабочее место на пару часов",
    "park": "пройдите круг по главной аллее",
    "garden": "пройдитесь по дорожкам",
    "playground": "дайте ребёнку побегать",
    "viewpoint": "сделайте панорамное фото города",
    "attraction": "осмотрите снаружи и сделайте фото",
    "artwork": "сделайте фото на фоне арт-объекта",
    "fountain": "посмотрите фонтан и сделайте фото",
    "memorial": "осмотрите монумент",
    "monument": "осмотрите монумент",
    "mall": "погрейтесь и загляните на фуд-корт",
    "zoo": "начните с главного павильона",
    "aquarium": "начните с главного туннеля",
    "ice_rink": "возьмите коньки напрокат",
    "escape_game": "запишитесь на ближайший свободный слот",
    "bowling": "возьмите дорожку на час",
    "trampoline_park": "возьмите сеанс на 30–60 минут",
}
EVENT_IDEAS = {"exhibition": "пройдите выставку, ~40–60 мин", "concert": "приходите за 15 минут до начала",
               "performance": "приходите за 15 минут до начала", "festival": "начните с главной сцены",
               "workshop": "уточните, нужна ли запись", "lecture": "приходите к началу",
               "party": "уточните вход на месте", "standup": "приходите заранее, лучшие места занимают",
               "film": "приходите к началу сеанса", "kids": "уточните возраст участников"}

NEED_LABELS = {
    "coffee": "кофе", "food": "поесть", "socket": "розетка", "wifi": "Wi‑Fi", "quiet": "тихо", "kids": "с ребёнком",
    "playground": "детская площадка", "interactive": "интерактив", "museum": "музей", "culture": "культура",
    "park": "парк", "walk": "прогулка", "view": "вид", "unusual": "необычное", "night": "ночью",
    "nightlife": "бар/клуб", "free": "бесплатно", "indoor": "в помещении", "accessible": "доступно для колясок",
    "romantic": "свидание", "photo": "фото", "shopping": "шопинг", "event": "события",
}

QUIET_SUBS = {"library", "museum", "gallery", "coworking", "garden", "books"}
NOISY_SUBS = {"bar", "pub", "nightclub", "karaoke", "stadium", "mall", "fast_food", "food_court", "arcade", "trampoline_park", "bowling"}
KIDS_YES = {"playground", "zoo", "aquarium", "trampoline_park", "theme_park", "water_park", "ice_cream"}
KIDS_LIKELY = {"museum", "mall", "park", "garden", "cinema", "planetarium", "ice_rink", "arcade", "bowling", "cafe",
               "fountain", "minigolf"}
ADULT_ONLY = {"bar", "pub", "nightclub", "hookah"}
WALK_SUBS = {"park", "garden", "viewpoint", "artwork", "memorial", "monument", "fountain", "attraction"}
PUBLIC_FREE = {"park", "garden", "viewpoint", "artwork", "memorial", "monument", "fountain", "playground"}
UNUSUAL_SUBS = {"artwork", "escape_game", "planetarium", "viewpoint", "attraction", "theme_park", "arts_centre"}
PHOTO_SUBS = {"viewpoint", "attraction", "artwork", "memorial", "monument", "fountain"}
ROMANTIC_SUBS = {"viewpoint", "restaurant", "theatre", "garden", "park"}


@dataclass
class Cand:
    """A place or an event, as the ranker sees it."""
    kind: str                       # place | event
    id: str
    title: str
    lat: float
    lon: float
    category: str = ""
    subcategory: str = ""
    indoor: str = "unknown"
    attrs: dict = field(default_factory=dict)
    description: str | None = None
    open_state: str = "unknown"     # open | closed | unknown (at query time)
    closes_at: datetime | None = None
    opens_at: datetime | None = None
    source_url: str | None = None
    event: dict | None = None       # whats_on() row for events
    attached: list[dict] = field(default_factory=list)  # events happening inside this place
    confidence: float = 0.8


def visit_range(c: Cand) -> tuple[int, int]:
    if c.kind == "event" and c.event:
        return EVENT_VISIT.get(c.event.get("kind") or "other", (60, 90))
    return VISIT.get(c.subcategory, (30, 45))


def _desc(c: Cand) -> str:
    parts = [c.description or "", str(c.attrs.get("description") or "")]
    parts += [str(e.get("description") or "") for e in c.attached]
    return " ".join(parts).lower()


def need_match(c: Cand, need: str, *, at: datetime | None = None, cold: bool = False) -> tuple[str, str]:
    """(certainty, evidence) for one need."""
    sub, cat, a = c.subcategory, c.category, c.attrs
    ev_kind = (c.event or {}).get("kind")
    labels = set((c.event or {}).get("labels") or [])
    attached_kinds = {e.get("kind") for e in c.attached}

    if c.kind == "event":
        k = ev_kind
        table = {
            "culture": ("yes", "событие культурной программы") if k in {"exhibition", "performance", "concert", "lecture", "film"} else ("unknown", ""),
            "event": ("yes", "событие из афиши"),
            "kids": ("yes", "детское событие") if k == "kids" else ("no", "") if k == "party" else ("likely", "обычно подходит семьям") if k in {"exhibition", "festival", "fair", "film"} else ("unknown", ""),
            "night": ("yes", "ночное событие") if "night" in labels else ("likely", "вечернее событие") if "evening" in labels else ("no", ""),
            "nightlife": ("yes", "вечеринка") if k == "party" else ("likely", "") if k in {"concert", "standup"} else ("no", ""),
            "unusual": ("likely", "разовое событие") if k in {"festival", "fair", "party", "standup", "workshop"} or "temporary" in labels or "today_only" in labels else ("unknown", ""),
            "free": ("yes", "вход бесплатный (источник)") if (c.event or {}).get("is_free") else ("unknown", ""),
            "indoor": ("likely", "проходит в помещении") if k in {"exhibition", "performance", "concert", "lecture", "film", "standup", "workshop"} else ("unknown", ""),
            "quiet": ("likely", "выставка — тихий формат") if k in {"exhibition", "lecture"} else ("no", "") if k in {"concert", "party", "festival"} else ("unknown", ""),
            "museum": ("likely", "выставка") if k == "exhibition" else ("unknown", ""),
            "interactive": ("yes", "интерактив (описание)") if "интерактив" in _desc(c) else ("likely", "") if k in {"workshop", "kids"} else ("unknown", ""),
            "photo": ("likely", "") if k in {"exhibition", "festival", "fair"} else ("unknown", ""),
            "walk": ("likely", "проходит под открытым небом") if k in {"festival", "fair", "excursion"} else ("unknown", ""),
            "romantic": ("likely", "") if k in {"concert", "performance", "exhibition"} else ("unknown", ""),
        }
        return table.get(need, ("unknown", ""))

    # ---- places ----
    if need == "coffee":
        if sub == "cafe":
            return "yes", "кофейня/кафе"
        if sub in {"restaurant", "fast_food", "food_court", "coworking", "mall"}:
            return "likely", "обычно есть кофе"
        return "no" if sub in {"playground", "artwork", "memorial", "monument", "viewpoint", "fountain"} else "unknown", ""
    if need == "food":
        if cat == "food":
            return "yes", "заведение питания"
        return ("likely", "есть фуд-корт или кафе") if sub in {"mall", "theme_park", "zoo"} else ("unknown", "")
    if need == "wifi":
        if a.get("wifi") or a.get("internet_access") in ("wlan", "yes", "wifi"):
            return "yes", "Wi‑Fi (OpenStreetMap)"
        return ("likely", "") if sub in {"coworking", "mall", "library"} else ("unknown", "")
    if need == "socket":
        if sub == "coworking":
            return "yes", "коворкинг"
        if a.get("wifi") or a.get("internet_access") in ("wlan", "yes", "wifi"):
            return "likely", "есть Wi‑Fi — розетки обычно есть, но не гарантированы"
        return ("likely", "") if sub in {"library", "cafe"} else ("unknown", "")
    if need == "quiet":
        if sub in QUIET_SUBS:
            return ("yes" if sub in {"library", "coworking"} else "likely"), {
                "museum": "музей", "gallery": "галерея", "garden": "сад", "library": "библиотека",
                "coworking": "коворкинг"}.get(sub, sub)
        if sub in NOISY_SUBS:
            return "no", ""
        return ("likely", "небольшое кафе") if sub == "cafe" else ("unknown", "")
    if need == "kids":
        if sub in ADULT_ONLY:
            return "no", ""
        if sub in KIDS_YES or "kids" in attached_kinds:
            return "yes", "подходит детям"
        if "интерактив" in _desc(c) or a.get("interactive") == "yes":
            return "yes", "интерактивные экспозиции"
        return ("likely", "семейное место") if sub in KIDS_LIKELY else ("unknown", "")
    if need == "playground":
        return ("yes", "детская площадка") if sub in {"playground", "trampoline_park"} else ("unknown", "")
    if need == "interactive":
        if "интерактив" in _desc(c) or a.get("interactive") == "yes":
            return "yes", "интерактивные экспозиции"
        return ("likely", "") if sub in {"planetarium", "escape_game", "trampoline_park"} else ("unknown", "")
    if need == "museum":
        return ("yes", "музей") if sub == "museum" else ("likely", "галерея") if sub == "gallery" else ("unknown", "")
    if need == "culture":
        if cat == "culture":
            return "yes", {"museum": "музей", "gallery": "галерея", "theatre": "театр"}.get(sub, "культурное место")
        if c.attached:
            return "yes", "сейчас там событие"
        return ("likely", "достопримечательность") if cat == "landmark" else ("unknown", "")
    if need in ("park", "walk"):
        if sub in {"park", "garden"}:
            return "yes", "парк"
        if need == "walk" and sub in WALK_SUBS:
            return "likely", "по пути прогулки"
        if need == "walk" and sub == "mall" and cold:
            return "likely", "можно пройтись под крышей"
        if need == "walk" and cold and c.indoor == "indoor" and cat in ("food", "culture"):
            return "likely", "остановка под крышей по пути"
        return "unknown", ""
    if need == "view":
        if sub == "viewpoint" or a.get("view") == "yes":
            return "yes", "панорамный вид"
        return ("likely", "") if sub == "attraction" else ("unknown", "")
    if need == "unusual":
        if c.attached:
            return "likely", "сейчас там особое событие"
        if a.get("interactive") == "yes" or "интерактив" in _desc(c):
            return "likely", "интерактивные экспозиции"
        return ("likely", "необычное место") if sub in UNUSUAL_SUBS else ("unknown", "")
    if need == "night":
        if cat == "nightlife":
            return "yes", "ночное заведение"
        close_h = c.closes_at.astimezone(settings.TZ).hour if c.closes_at else None
        if close_h is not None and close_h < 6:
            return "yes", "работает после полуночи"
        return ("likely", "открыто допоздна") if close_h is not None and close_h >= 22 else ("unknown", "")
    if need == "nightlife":
        return ("yes", "бар/клуб") if cat == "nightlife" else ("unknown", "")
    if need == "free":
        if a.get("fee") == "no":
            return "yes", "вход свободный (OpenStreetMap)"
        if sub in PUBLIC_FREE:
            return "likely", "общественное место"
        return "unknown", ""
    if need == "indoor":
        return {"indoor": ("yes", "в помещении"), "mixed": ("likely", "частично в помещении"),
                "outdoor": ("no", "")}.get(c.indoor, ("unknown", ""))
    if need == "accessible":
        w = a.get("wheelchair")
        return {"yes": ("yes", "доступно для колясок (OpenStreetMap)"), "limited": ("likely", "частично доступно"),
                "no": ("no", "")}.get(w, ("unknown", ""))
    if need == "romantic":
        return ("likely", "") if sub in ROMANTIC_SUBS else ("unknown", "")
    if need == "photo":
        return ("yes", "фотогеничное место") if sub in PHOTO_SUBS or a.get("view") == "yes" else ("unknown", "")
    if need == "shopping":
        return ("yes", "торговый центр") if sub in {"mall", "marketplace"} else ("unknown", "")
    if need == "event":
        return ("yes", "сейчас там событие") if c.attached else ("unknown", "")
    return "unknown", ""


def idea(c: Cand) -> tuple[str, str]:
    """(what to do there, source): from the event description when we have it, else an assistant idea."""
    for e in ([c.event] if c.event else []) + c.attached:
        d = (e or {}).get("description")
        if d:
            first = re.split(r"(?<=[.!?])\s", d.strip())[0]
            return first[:180], "data"
    if c.kind == "event":
        return EVENT_IDEAS.get((c.event or {}).get("kind") or "", "загляните, пока идёт"), "idea"
    return IDEAS.get(c.subcategory, "осмотритесь и сделайте фото"), "idea"
