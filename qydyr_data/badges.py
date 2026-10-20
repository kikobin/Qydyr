"""Machine labels from whats_on()/places_near() -> short human Russian phrases for the UI and the LLM.

    "Идёт сейчас · вечерняя выставка · до 21:00 · последние дни (до 26 октября)"
"""
from __future__ import annotations

from datetime import datetime, timedelta

from . import settings

MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября",
              "октября", "ноября", "декабря"]
KIND_TEMPORARY = {"exhibition": "временная выставка", "festival": "фестиваль, идёт несколько дней",
                  "fair": "ярмарка, идёт несколько дней"}
KIND_EVENING = {"exhibition": "вечерняя выставка", "concert": "вечерний концерт", "performance": "вечерний спектакль",
                "lecture": "вечерняя лекция", "film": "вечерний показ", "excursion": "вечерняя экскурсия"}


def _hm(dt: datetime) -> str:
    return dt.astimezone(settings.TZ).strftime("%H:%M")


def _day(d) -> str:
    return f"{d.day} {MONTHS_GEN[d.month - 1]}"


def _minutes(n: int) -> str:
    if n < 60:
        return f"{n} мин"
    h, m = divmod(n, 60)
    return f"{h} ч {m} мин" if m else f"{h} ч"


def confidence_word(c: float) -> str:
    # structured data ~0.85, a verified page ~0.76, a Telegram post ~0.5
    return "высокая" if c >= 0.75 else "средняя" if c >= 0.5 else "низкая"


def _start_phrase(row: dict) -> str:
    """"Через 40 мин, в 20:00" for the next hours; "Завтра в 10:00" when it is another day."""
    start = row["starts_at"].astimezone(settings.TZ)
    mins = row.get("minutes_to_start")
    if mins is not None:
        ref = (row["starts_at"] - timedelta(minutes=mins)).astimezone(settings.TZ)
        if start.date() != ref.date():
            return (f"Завтра в {start:%H:%M}" if (start.date() - ref.date()).days == 1
                    else f"{_day(start.date())} в {start:%H:%M}")
        if mins > 180:
            return f"Сегодня в {start:%H:%M}"
        return f"Через {_minutes(mins)}, в {start:%H:%M}"
    return f"Сегодня в {start:%H:%M}"


def _in_title(phrase: str, title: str | None) -> bool:
    """'вечерняя выставка' adds nothing to 'Вечерняя выставка «Ночь в музее»'."""
    return bool(title) and all(w.lower()[:5] in title.lower() for w in phrase.split())


def describe_event(row: dict, title: str | None = None) -> str:
    labels = set(row.get("labels") or [])
    kind = row.get("kind")
    title = title if title is not None else row.get("title")
    parts: list[str] = []
    st = row["state"]
    if st == "now":
        parts.append("Идёт сейчас")
    elif st == "soon":
        parts.append(_start_phrase(row))
    elif st == "later_today":
        parts.append(f"Сегодня в {_hm(row['starts_at'])}")
    else:
        parts.append("Сегодня, время уточняйте")

    if "evening" in labels and kind in KIND_EVENING:
        if not _in_title(KIND_EVENING[kind], title):
            parts.append(KIND_EVENING[kind])
    elif "evening" in labels:
        parts.append("вечернее")
    if "night" in labels:
        parts.append("ночное")
    if "temporary" in labels and not _in_title(KIND_TEMPORARY.get(kind, "временное событие"), title):
        parts.append(KIND_TEMPORARY.get(kind, "временное событие"))
    if "today_only" in labels:
        parts.append("только сегодня")

    if st == "now" and row.get("minutes_left") is not None and "date_only" not in labels:
        parts.append(f"успейте: осталось {_minutes(row['minutes_left'])}" if "ends_soon" in labels
                     else f"до {_hm(row['ends_at'])}")
    if "last_days" in labels and row.get("ends_on"):
        left = row.get("days_left")
        tail = "последний день" if left == 0 else f"последние дни (до {_day(row['ends_on'])})"
        parts.append(tail)
    if "free" in labels:
        parts.append("бесплатно")
    if "venue_hours" in labels:
        parts.append("время по графику площадки")
    if "end_estimated" in labels and st != "now":
        parts.append("окончание примерное")
    return " · ".join(parts)


def describe_place(row: dict) -> str:
    st = row["open_state"]
    if st == "unknown":
        return "часы работы неизвестны"
    if st == "open":
        left = row.get("minutes_to_close")
        if left is not None and left > 24 * 60:
            return "открыто, круглосуточно"
        if left is not None and left <= 45:
            return f"открыто, закроется через {_minutes(left)}"
        return f"открыто до {_hm(row['closes_at'])}"
    if row.get("opens_at"):
        mins = row.get("minutes_to_open")
        if mins is not None and mins <= 120:
            return f"закрыто, откроется через {_minutes(mins)}"
        opens = row["opens_at"].astimezone(settings.TZ)
        return f"закрыто, откроется {_day(opens.date())} в {opens.strftime('%H:%M')}"
    return "закрыто"
