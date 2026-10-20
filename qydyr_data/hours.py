"""Opening hours (OSM syntax) -> concrete open intervals, aware of Kazakhstan public holidays.

The same syntax is used for venues (from OSM) and for event schedules extracted from text,
e.g. "ежедневно с 10:00 до 18:00, кроме понедельника; последний вторник — санитарный день"
-> "Tu-Su 10:00-18:00; Tu[-1] off".
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

from opening_hours import OpeningHours, State

from . import settings

log = logging.getLogger(__name__)


def parse(text: str | None) -> OpeningHours | None:
    if not text or not text.strip():
        return None
    try:
        return OpeningHours(text.strip(), country=settings.COUNTRY)
    except Exception:  # unknown country support or parser error
        try:
            return OpeningHours(text.strip())
        except Exception as e:
            log.debug("unparseable opening_hours %r: %s", text, e)
            return None


def is_valid(text: str | None) -> bool:
    return parse(text) is not None


def open_intervals(text_or_oh, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Open intervals between start and end (aware datetimes in settings.TZ), merged."""
    oh = parse(text_or_oh) if isinstance(text_or_oh, str) else text_or_oh
    if oh is None:
        return []
    tz = settings.TZ
    s_local = start.astimezone(tz).replace(tzinfo=None)
    e_local = end.astimezone(tz).replace(tzinfo=None)
    out: list[tuple[datetime, datetime]] = []
    for a, b, state, _comment in oh.intervals(s_local, e_local):
        if state != State.OPEN:
            continue
        a_aw, b_aw = a.replace(tzinfo=tz), b.replace(tzinfo=tz)
        if out and out[-1][1] >= a_aw:  # merge touching intervals (e.g. 24/7 split at midnight)
            out[-1] = (out[-1][0], max(out[-1][1], b_aw))
        else:
            out.append((a_aw, b_aw))
    return out


def day_intervals(text_or_oh, day: date) -> list[tuple[datetime, datetime]]:
    """Open intervals belonging to a local day.

    An overnight 18:00-04:00 belongs to the day it starts (kept whole); the 00:00-04:00 tail of the
    previous night is not today's. A 24/7 place yields the whole day.
    """
    tz = settings.TZ
    d0 = datetime.combine(day, time.min, tzinfo=tz)
    d1 = d0 + timedelta(days=1)
    out: list[tuple[datetime, datetime]] = []
    for a, b in open_intervals(text_or_oh, d0 - timedelta(days=1), d1 + timedelta(days=1)):
        if d0 <= a < d1:
            out.append((a, b))
        elif a < d0 and b > d1:
            out.append((d0, d1))
    return out
