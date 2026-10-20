"""Turn an extracted event into concrete time windows (sessions).

- concert on 25 Oct at 19:00                 -> one session 19:00–21:00 (end estimated if unknown)
- exhibition 10 Oct – 30 Nov, "Tu-Su 10-18"  -> one session per open day, from the event's own schedule
- same exhibition without hours              -> per day, using the venue's opening hours (hours_from=venue)
- no hours anywhere                          -> date-only sessions (time_known=False): "сегодня, время уточняйте"
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from .. import hours, settings
from .models import ExtractedEvent, Session

DEFAULT_MINUTES = {
    "concert": 120, "performance": 150, "festival": 240, "fair": 360, "workshop": 120, "lecture": 90,
    "party": 240, "excursion": 90, "sport": 120, "kids": 90, "film": 120, "standup": 120, "other": 120,
    "exhibition": 120,
}
VENUE_HOURS_KINDS = {"exhibition", "fair"}


def parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def parse_time(s: str | None) -> time | None:
    if not s:
        return None
    s = s.strip().replace(".", ":")
    try:
        hh, mm = s.split(":")[:2]
        if hh == "24" and mm == "00":
            return time(23, 59)
        return time(int(hh), int(mm))
    except (ValueError, TypeError):
        return None


def _at(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=settings.TZ)


def _timed(d: date, t1: time, t2: time | None, kind: str) -> Session:
    start = _at(d, t1)
    if t2 is not None:
        end = _at(d, t2)
        if end <= start:  # 22:00–02:00
            end += timedelta(days=1)
        return Session(starts_at=start, ends_at=end)
    return Session(starts_at=start, ends_at=start + timedelta(minutes=DEFAULT_MINUTES.get(kind, 120)),
                   end_estimated=True)


def _date_only(d: date) -> Session:
    start = _at(d, time.min)
    return Session(starts_at=start, ends_at=start + timedelta(days=1), time_known=False, hours_from="none")


def build_sessions(ev: ExtractedEvent, kind: str, *, venue_hours: str | None, today: date,
                   horizon_days: int = settings.EVENTS_HORIZON_DAYS) -> list[Session]:
    last_day = today + timedelta(days=horizon_days)
    t1, t2 = parse_time(ev.time_start), parse_time(ev.time_end)
    d1, d2 = parse_date(ev.date_start), parse_date(ev.date_end)
    out: list[Session] = []

    # 1. explicit sessions
    for s in ev.sessions:
        d = parse_date(s.date)
        if not d or d < today - timedelta(days=1) or d > last_day:
            continue
        st, en = parse_time(s.time_start), parse_time(s.time_end)
        out.append(_timed(d, st, en, kind) if st else _date_only(d))
    if out:
        return _dedup(out)

    multi_day = (d1 and d2 and d2 > d1) or (d2 and not d1)
    if multi_day and kind in VENUE_HOURS_KINDS and t1 and not t2:
        t1 = None  # "открытие 9 октября в 16:00" is the vernissage, not a daily 16:00–18:00 window
    if multi_day or (ev.schedule_opening_hours and not t1):
        # 2. a period: one window per day
        start_day = max(d1 or today, today)
        end_day = min(d2 or last_day, last_day)
        rule, src = None, "none"
        if ev.schedule_opening_hours and hours.is_valid(ev.schedule_opening_hours):
            rule, src = hours.parse(ev.schedule_opening_hours), "event"
        # Venue hours fit something you can walk into any time (an exhibition, a fair), not a series
        # of screenings or shows listed as one date range ("Киноужин", 18.10–28.11).
        elif venue_hours and hours.is_valid(venue_hours) and not t1 and kind in VENUE_HOURS_KINDS:
            rule, src = hours.parse(venue_hours), "venue"
        day = start_day
        while day <= end_day:
            if t1:
                out.append(_timed(day, t1, t2, kind))
            elif rule is not None:
                for a, b in hours.day_intervals(rule, day):
                    out.append(Session(starts_at=a, ends_at=b, hours_from=src))
            else:
                out.append(_date_only(day))
            day += timedelta(days=1)
        return _dedup(out)

    # 3. a single day
    d = d1 or d2
    if not d or d < today - timedelta(days=1) or d > last_day:
        return []
    if t1:
        return [_timed(d, t1, t2, kind)]
    if venue_hours and kind == "exhibition" and hours.is_valid(venue_hours):
        return [Session(starts_at=a, ends_at=b, hours_from="venue") for a, b in hours.day_intervals(venue_hours, d)] \
            or [_date_only(d)]
    return [_date_only(d)]


def _dedup(sessions: list[Session]) -> list[Session]:
    seen: dict[datetime, Session] = {}
    for s in sessions:
        if s.starts_at not in seen or (s.time_known and not seen[s.starts_at].time_known):
            seen[s.starts_at] = s
    return sorted(seen.values(), key=lambda s: s.starts_at)
