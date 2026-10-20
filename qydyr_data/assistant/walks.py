"""Ready-made themed walks around where you are ("Прогулки", like Yandex Maps' city walks) — but
assembled for right now: real opening hours, events and weather, not a fixed list made last year."""
from __future__ import annotations

from datetime import datetime

from .models import Anchor, Overrides
from .recommend import Assistant

THEMES = [
    {"id": "highlights", "title": "Главное рядом", "subtitle": "достопримечательности и виды для фото",
     "text": "что посмотреть рядом, достопримечательности и фото", "budget": (60, 90),
     "needs": ["view", "photo", "culture"]},
    {"id": "warm", "title": "Тёплый маршрут", "subtitle": "почти всё время под крышей",
     "text": "погулять в тепле, в помещении, культура и кофе", "budget": (60, 90),
     "needs": ["indoor", "culture", "coffee"]},
    {"id": "kids", "title": "С ребёнком", "subtitle": "интерактив, площадки, перекус",
     "text": "с ребёнком: интерактив и площадка", "budget": (60, 90), "needs": ["kids", "interactive", "playground"],
     "with_kids": True},
    {"id": "events", "title": "Что идёт сегодня", "subtitle": "только события из афиш",
     "text": "события и выставки сегодня", "budget": (90, 120), "needs": ["event", "culture"], "only_events": True},
    {"id": "free", "title": "Бесплатно", "subtitle": "парки, арт-объекты, свободный вход",
     "text": "бесплатно погулять и сфотографироваться", "budget": (45, 60), "needs": ["free", "walk", "photo"]},
]


# Titles in the other interface languages (the Kazakh is worth a native speaker's read).
LOCAL = {
    "kk": {
        "highlights": ("Жақындағы көрікті жерлер", "көрікті жерлер мен фотоға әдемі көріністер"),
        "warm": ("Жылы маршрут", "көбіне жабық жерде"),
        "kids": ("Баламен", "интерактив, ойын алаңдары, жеңіл тамақ"),
        "events": ("Бүгін не өтеді", "тек афишадағы іс-шаралар"),
        "free": ("Тегін", "саябақтар, арт-нысандар, кіру тегін"),
    },
    "en": {
        "highlights": ("Highlights nearby", "sights and photo spots"),
        "warm": ("Warm route", "mostly indoors"),
        "kids": ("With a kid", "interactive, playgrounds, snacks"),
        "events": ("On today", "only events from the listings"),
        "free": ("Free", "parks, art objects, free entry"),
    },
}


def walk_meta(stops: int, minutes: int, outdoor: int | None, lang: str = "ru") -> str:
    if lang == "kk":
        meta = f"{stops} нүкте · {minutes} мин"
        return meta + (f" · далада ~{outdoor} мин" if outdoor is not None else "")
    if lang == "en":
        meta = f"{stops} {'stop' if stops == 1 else 'stops'} · {minutes} min"
        return meta + (f" · ~{outdoor} min outside" if outdoor is not None else "")
    meta = f"{stops} {'точки' if 2 <= stops <= 4 else 'точка' if stops == 1 else 'точек'} · {minutes} мин"
    return meta + (f" · на улице ~{outdoor} мин" if outdoor is not None else "")


def build_walks(assistant: Assistant, anchor: Anchor, at: datetime, lang: str = "ru", **ask_kw) -> list[dict]:
    out = []
    for t in THEMES:
        lo, hi = t["budget"]
        ov = Overrides(budget_min=lo, budget_max=hi, add_needs=t["needs"], with_kids=t.get("with_kids"),
                       only_events=t.get("only_events"))
        ans = assistant.ask(t["text"], at=at, anchor=anchor.model_copy(), overrides=ov, **ask_kw)
        if not ans.items:
            continue
        r = ans.route
        stops = len(r.stops) if r else len(ans.items)
        minutes = r.total_min if r else ans.items[0].visit_min[1] + ans.items[0].walk_min
        outdoor = r.outdoor_min if r else None
        title, subtitle = LOCAL.get(lang, {}).get(t["id"], (t["title"], t["subtitle"]))
        out.append({"id": t["id"], "title": title, "subtitle": subtitle,
                    "meta": walk_meta(stops, minutes, outdoor, lang), "answer": ans.model_dump(mode="json")})
    return out
