"""What the assistant knows about a place or event, and how sure it is.

Every match is tri-state with evidence: yes (data says so), likely (typical for this kind of place),
unknown, no. "Розетки: вероятно — кафе с Wi‑Fi" is honest; "есть розетки" would be invented.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from .. import settings
from .intent import event_kind_fits

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
    "stadium": (90, 150), "dance": (60, 90), "books": (20, 40), "souvenir": (15, 30), "kids_centre": (60, 90),
    "anticafe": (60, 120), "vr": (45, 60),
}
EVENT_VISIT = {"exhibition": (40, 60), "concert": (90, 120), "performance": (120, 150), "festival": (60, 120),
               "fair": (45, 90), "workshop": (90, 120), "lecture": (60, 90), "party": (120, 180),
               "excursion": (60, 90), "sport": (90, 120), "kids": (60, 90), "film": (100, 130), "standup": (90, 120),
               "other": (60, 90)}

IDEAS = {
    "kids_centre": "дайте ребёнку поиграть, а сами передохните рядом",
    "anticafe": "возьмите настольную игру и чай: платите за время, а не за меню",
    "vr": "запишитесь на ближайший сеанс: обычно это 30–60 минут",
    "museum": "начните с главного зала, потом — временная экспозиция",
    "gallery": "посмотрите текущую выставку целиком, это не займёт много времени",
    "theatre": "проверьте в кассе спектакли на сегодня",
    "cinema": "посмотрите ближайшие сеансы на месте",
    "planetarium": "попадите на ближайший сеанс в куполе",
    "library": "найдите тихий читальный зал",
    "cafe": "возьмите горячий напиток и займите место у окна",
    "restaurant": "спросите, какое блюдо здесь заказывают чаще всего",
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
    "romantic": "свидание", "photo": "фото", "shopping": "шопинг", "event": "события", "cheap": "недорого",
    "dog": "с собакой",
}

QUIET_SUBS = {"library", "museum", "gallery", "coworking", "garden", "books"}
NOISY_SUBS = {"bar", "pub", "nightclub", "karaoke", "stadium", "mall", "fast_food", "food_court", "arcade", "trampoline_park",
              "bowling", "kids_centre", "vr"}
KIDS_YES = {"playground", "zoo", "aquarium", "trampoline_park", "theme_park", "water_park", "ice_cream", "kids_centre"}
KIDS_LIKELY = {"museum", "mall", "park", "garden", "cinema", "planetarium", "ice_rink", "arcade", "bowling", "cafe",
               "fountain", "minigolf"}
ADULT_ONLY = {"bar", "pub", "nightclub", "hookah"}
WALK_SUBS = {"park", "garden", "viewpoint", "artwork", "memorial", "monument", "fountain", "attraction"}
PUBLIC_FREE = {"park", "garden", "viewpoint", "artwork", "memorial", "monument", "fountain", "playground"}
UNUSUAL_SUBS = {"artwork", "escape_game", "planetarium", "viewpoint", "attraction", "theme_park", "arts_centre"}
PHOTO_SUBS = {"viewpoint", "attraction", "artwork", "memorial", "monument", "fountain"}
ROMANTIC_SUBS = {"viewpoint", "restaurant", "theatre", "garden", "park"}
# places where an event kind usually is, for "хочу на выставку" when no listing says so
KIND_SUBS = {"exhibition": {"museum", "gallery", "exhibition_centre"},
             "concert": {"concert_hall", "events_venue"}, "performance": {"theatre"}, "film": {"cinema"}}


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
    inside: dict | None = None      # the closed mall this place (hours unknown) is in


def indoor_attraction(c: Cand) -> bool:
    """A ride or an art object with a floor number (OSM level=4) is inside a building, usually a mall:
    not a landmark to walk to, photograph at sunset or "осмотреть снаружи"."""
    return c.kind == "place" and c.subcategory in WALK_SUBS | PHOTO_SUBS and "level" in c.attrs


def kind_fit(c: Cand, kinds: list[str]) -> bool:
    """Does it offer the kind of event asked for ("концерт", "выставка")? Anything does when none was asked."""
    if not kinds:
        return True
    if c.kind == "event":
        return event_kind_fits(c.event or {}, kinds)
    return any(event_kind_fits(e, kinds) for e in c.attached) or any(c.subcategory in KIND_SUBS.get(k, ()) for k in kinds)


def visit_range(c: Cand) -> tuple[int, int]:
    if c.kind == "event" and c.event:
        return EVENT_VISIT.get(c.event.get("kind") or "other", (60, 90))
    return VISIT.get(c.subcategory, (30, 45))


def _src(a: dict, key: str) -> str:
    """Where a fact about the place comes from: OpenStreetMap, or 2ГИС where OSM had nothing."""
    return "2ГИС" if a.get(f"{key}:source") == "2gis" else "OpenStreetMap"


def rating(c: Cand) -> tuple[float, int] | None:
    """2ГИС rating and number of reviews, when there are enough reviews to mean something."""
    r, n = c.attrs.get("2gis:rating"), c.attrs.get("2gis:reviews")
    return (float(r), int(n)) if r is not None and n and int(n) >= 15 else None


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
    if need in ("park", "walk", "view", "photo", "free", "culture") and indoor_attraction(c):
        return "unknown", ""
    if need == "coffee":
        if sub == "cafe":
            return "yes", "кофейня/кафе"
        if sub in {"restaurant", "fast_food", "food_court", "coworking", "mall", "anticafe"}:
            return "likely", "обычно есть кофе"
        return "no" if sub in {"playground", "artwork", "memorial", "monument", "viewpoint", "fountain"} else "unknown", ""
    if need == "food":
        if cat == "food":
            return "yes", "заведение питания"
        return ("likely", "есть фуд-корт или кафе") if sub in {"mall", "theme_park", "zoo"} else ("unknown", "")
    if need == "wifi":
        if a.get("wifi") or a.get("internet_access") in ("wlan", "yes", "wifi"):
            return "yes", f"Wi‑Fi ({_src(a, 'wifi')})"
        return ("likely", "") if sub in {"coworking", "mall", "library"} else ("unknown", "")
    if need == "socket":
        if sub == "coworking":
            return "yes", "коворкинг"
        if a.get("socket") == "yes":
            return "yes", "розетки (2ГИС)"
        if a.get("laptop") == "yes":
            return "yes", "можно с ноутбуком (2ГИС)"
        if sub == "anticafe":
            return "likely", "антикафе: столы и розетки обычно есть"
        if a.get("wifi") or a.get("internet_access") in ("wlan", "yes", "wifi"):
            return "likely", "есть Wi‑Fi — розетки обычно есть, но не гарантированы"
        return ("likely", "") if sub in {"library", "cafe"} else ("unknown", "")
    if need == "quiet":
        # a concert or a stand-up going on there right now is the opposite of quiet, whatever the place is
        if any(e.get("state") in ("now", "soon") and event_kind_fits(e, ["concert", "standup", "party", "festival"])
               for e in c.attached):
            return "no", ""
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
        if a.get("kids_friendly") == "kids_area":
            return "yes", "детская зона (2ГИС)"
        if a.get("kids_friendly") == "kids_menu":
            return "likely", "детское меню (2ГИС)"
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
            return "yes", "панорамный вид" + (" (2ГИС)" if a.get("view:source") == "2gis" else "")
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
        if cat == "nightlife":
            return "yes", "бар/клуб"
        return ("likely", "живая музыка (2ГИС)") if a.get("live_music") == "yes" else ("unknown", "")
    if need == "free":
        if a.get("fee") == "no":
            return "yes", "вход свободный (OpenStreetMap)"
        if sub in PUBLIC_FREE:
            return "likely", "общественное место"
        return "unknown", ""
    if need == "cheap":
        check = a.get("avg_check")
        local = at.astimezone(settings.TZ) if at else None
        lunch = a.get("business_lunch") == "yes" and local is not None and local.weekday() < 5 and 11 <= local.hour < 16
        if check:
            label = f"средний чек {int(check):,} ₸ (2ГИС)".replace(",", " ")
            if check > 6000 and lunch:   # a dear restaurant, but its weekday lunch is not
                return "likely", f"бизнес-ланч (2ГИС), хотя {label}"
            return ("yes", label) if check <= 3500 else ("likely", label) if check <= 6000 else ("no", label)
        if lunch:
            return "likely", "бизнес-ланч (2ГИС)"
        if sub in PUBLIC_FREE or a.get("fee") == "no":
            return "yes", "бесплатно"
        return ("likely", "обычно недорого") if sub in {"fast_food", "food_court", "anticafe", "ice_cream", "library"} else ("unknown", "")
    if need == "indoor":
        return {"indoor": ("yes", "в помещении"), "mixed": ("likely", "частично в помещении"),
                "outdoor": ("no", "")}.get(c.indoor, ("unknown", ""))
    if need == "accessible":
        w = a.get("wheelchair")
        if w == "yes" and a.get("access:features"):
            return "yes", ", ".join(a["access:features"][:3]).lower() + " (2ГИС)"
        if w is None and sub == "mall":   # OSM rarely marks it; a big mall in Astana is built with lifts and ramps
            return "likely", "крупный торговый центр: обычно лифты и пандусы, но в OpenStreetMap не отмечено"
        return {"yes": ("yes", f"отмечено в {_src(a, 'wheelchair')}"), "limited": ("likely", f"частично доступно ({_src(a, 'wheelchair')})"),
                "no": ("no", "")}.get(w, ("unknown", ""))
    if need == "romantic":
        if cat == "food" and a.get("view") == "yes":
            return "yes", "ресторан с панорамным видом" + (" (2ГИС)" if a.get("view:source") == "2gis" else "")
        if a.get("live_music") == "yes":
            return "likely", "живая музыка (2ГИС)"
        return ("likely", "") if sub in ROMANTIC_SUBS else ("unknown", "")
    if need == "dog":
        if a.get("dog") in ("yes", "leashed"):
            return "yes", f"можно с собакой ({_src(a, 'dog')})"
        if a.get("dog") == "no":
            return "no", ""
        if sub in {"park", "garden", "square", "promenade"} or cat == "park":
            return "likely", "парк: обычно можно с собакой на поводке"
        return "unknown", ""          # indoors it is up to the place: no data, no guess
    if need == "photo":
        return ("yes", "фотогеничное место") if sub in PHOTO_SUBS or a.get("view") == "yes" else ("unknown", "")
    if need == "shopping":
        return ("yes", "торговый центр") if sub in {"mall", "marketplace"} else ("unknown", "")
    if need == "event":
        return ("yes", "сейчас там событие") if c.attached else ("unknown", "")
    return "unknown", ""


# ticket sites glue lines together: "Сбор: 18:00Начало: 19:00Выступят: …"
_GLUED = re.compile(r"(\d{1,2}:\d{2})(?=[A-ZА-ЯЁӘҒҚҢӨҰҮҺІ])")


def idea(c: Cand) -> tuple[str, str]:
    """(what to do there, source): from the event description when we have it, else an assistant idea."""
    for e in ([c.event] if c.event else []) + c.attached:
        d = (e or {}).get("description")
        if d:
            d = _GLUED.sub(r"\1 · ", d)
            first = re.split(r"(?<=[.!?])\s", d.strip())[0]
            return first[:180], "data"
    if c.kind == "event":
        return EVENT_IDEAS.get((c.event or {}).get("kind") or "", "загляните, пока идёт"), "idea"
    if indoor_attraction(c):
        return "это внутри здания: уточните на месте, работает ли сейчас", "idea"
    if c.subcategory in ("restaurant", "cafe", "food_court") and (cu := cuisine(c)):
        return f"попробуйте блюда {cu} кухни", "idea"
    return IDEAS.get(c.subcategory, "осмотритесь и сделайте фото"), "idea"


CUISINE_RU = {"kazakh": "казахской", "italian": "итальянской", "pizza": "итальянской", "japanese": "японской",
              "sushi": "японской", "georgian": "грузинской", "uzbek": "узбекской", "chinese": "китайской",
              "korean": "корейской", "turkish": "турецкой", "european": "европейской", "asian": "азиатской",
              "russian": "русской", "american": "американской", "indian": "индийской", "thai": "тайской",
              "french": "французской", "mexican": "мексиканской", "vietnamese": "вьетнамской", "arab": "арабской",
              "lebanese": "ливанской", "caucasian": "кавказской", "oriental": "восточной", "uyghur": "уйгурской"}


def cuisine(c: Cand) -> str | None:
    """The kitchen in the genitive, for an idea: "европейской" (2ГИС "Европейская кухня", OSM cuisine=european)."""
    for name in c.attrs.get("2gis:cuisine") or []:
        word = name.split()[0].lower()
        if word.endswith("ая"):
            return word[:-2] + "ой"
    for code in str(c.attrs.get("cuisine") or "").lower().replace(",", ";").split(";"):
        if (ru := CUISINE_RU.get(code.strip())):
            return ru
    return None
