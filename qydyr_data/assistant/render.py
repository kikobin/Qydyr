"""Answer -> plain text (CLI, logs, the translator's input, the bot's fallback). Every item: where,
how far, why, how long, what to do there, and where the facts come from. Marks are words, not
pictures: "(из афиши)" — a fact from data, "(идея ассистента)" — the assistant's suggestion."""
from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlsplit

from .. import settings
from ..badges import confidence_word
from .models import Answer, Chip

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
SRC_MARK = {"data": "из афиши", "idea": "идея ассистента"}


def source_name(url: str) -> str:
    host = urlsplit(url).netloc.replace("www.", "")
    return "OpenStreetMap" if "openstreetmap" in host else host or url


def _hm(dt) -> str:
    return dt.astimezone(settings.TZ).strftime("%H:%M")


def chip_text(c: Chip) -> str:
    return c.label + ("?" if c.assumed else "")


def understood(ans: Answer) -> str:
    """'Понял так: …' — what the assistant took from the request; '?' marks its guesses."""
    return " · ".join(chip_text(c) for c in ans.chips if c.active)


def render(ans: Answer) -> str:
    head = []
    if ans.anchor:
        head.append(ans.anchor.label)
    head.append(f"{WEEKDAYS[ans.at.weekday()]} {ans.at:%H:%M}")
    if ans.weather:
        head.append(ans.weather)
    lines = [" · ".join(head)]
    if ans.chips:
        lines.append("Понял так: " + understood(ans))
    for n in ans.notes:
        lines.append(f"Важно: {n}")

    for i, it in enumerate(ans.items, 1):
        walk = "прямо здесь" if it.walk_min == 0 else f"{it.walk_min} мин {'на такси' if it.by == 'taxi' else 'пешком'}"
        lines += ["", f"{i}. {it.title} — {walk} ({it.distance_m} м)", f"   {it.status}"]
        if it.leave_by:
            lines.append("   Выходите сейчас, чтобы успеть" if it.leave_by <= ans.at + timedelta(minutes=1)
                         else f"   Выходите до {_hm(it.leave_by)}, чтобы успеть")
        if it.why:
            lines.append("   Почему: " + "; ".join(it.why))
        if it.group_fit:
            lines.append("   Каждому: " + " · ".join(it.group_fit))
        lo, hi = it.visit_min
        dur = f"{lo} мин" if lo == hi else f"{lo}–{hi} мин"
        lines.append(f"   Сколько: {dur}")
        lines.append(f"   Что делать ({SRC_MARK[it.what_to_do_source]}): {it.what_to_do}")
        if it.warnings:
            lines.append("   Внимание: " + "; ".join(it.warnings))
        src = ", ".join(dict.fromkeys(source_name(u) for u in it.sources)) or "база"
        lines.append(f"   Источник: {src} · уверенность: {confidence_word(it.confidence)}")

    if ans.route:
        r = ans.route
        lines += ["", f"Мини-маршрут на {r.total_min} мин (пешком {r.walk_min} мин, на улице ~{r.outdoor_min} мин):"]
        for s in r.stops:
            lines.append(f"   {_hm(s.arrive_at)}–{_hm(s.leave_at)}  {s.title}" + (f"  (+{s.walk_min} мин пешком)" if s.walk_min else ""))
    for g in ans.group_notes:
        lines.append(f"Группа: {g}")
    if ans.funnel:
        lines += ["", "Как выбирал: " + " → ".join(f"{f.n} {f.label}" for f in ans.funnel)]
    if ans.used_data:
        lines += ["Что я учёл: " + "; ".join(ans.used_data)]
    return "\n".join(lines)


def render_why_not(ans: Answer, limit: int = 6) -> str:
    if not ans.rejected:
        return "Других заметных вариантов рядом не было."
    rows = [f"{r.title} — {r.reason}" for r in ans.rejected[:limit]]
    return "Почему не другое:\n" + "\n".join(f"   {x}" for x in rows)
