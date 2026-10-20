"""RawEvent (one extraction) -> EventRecord (one row in `events`): venue, sessions, confidence, dedup."""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timezone
from urllib.parse import urlsplit

from .. import settings
from ..textnorm import basic, match_key
from .models import EventRecord, RawEvent
from .sessions import build_sessions, parse_date
from .venues import PlaceLite, VenueMatcher

TEMPORARY_KINDS = {"exhibition", "festival", "fair"}
KIND_WORDS = re.compile(
    r"\b(выставка|выставки|экспозиция|концерт|спектакль|фестиваль|мастер класс|мастеркласс|лекция|"
    r"стендап|stand up|показ|кинопоказ|премьера|ярмарка|маркет|вечеринка|экскурсия|exhibition|concert|"
    r"festival|көрме|концерті|қойылым)\b"
)
BASE_CONFIDENCE = {"jsonld": 0.9, "page": 0.8, "digest": 0.7, "post": 0.6}
QUOTED_RE = re.compile(r"[«\"“„]([^«»\"“”„]{2,})[»\"”“]")


def title_key(title: str) -> str:
    """Afishas write 'Вечерняя выставка «Ночь в музее»', Telegram writes 'Ночь в музее':
    the quoted part is the real name of the event."""
    quoted = QUOTED_RE.findall(title)
    base = " ".join(quoted) if quoted else title
    t = KIND_WORDS.sub(" ", basic(base))
    return match_key(t) or match_key(title)


def make_id(title: str, venue_key: str) -> str:
    return "ev_" + hashlib.sha1(f"{title_key(title)}|{venue_key}".encode()).hexdigest()[:16]


def confidence(raw: RawEvent, has_time: bool) -> float:
    c = BASE_CONFIDENCE.get(raw.mode, 0.6)
    c *= 0.75 + 0.25 * max(0.0, min(1.0, raw.reliability))
    if raw.extraction != "jsonld":
        c *= 1.0 if raw.evidence_ok else 0.75
        c *= {True: 1.0, False: 0.55, None: 0.92}[raw.dates_consistent]
    if not has_time:
        c *= 0.9
    return round(max(0.05, min(0.98, c)), 3)


def to_record(raw: RawEvent, place: PlaceLite | None, match_method: str | None, today: date,
              horizon_days: int = settings.EVENTS_HORIZON_DAYS) -> EventRecord | None:
    e = raw.event
    if not e.title or not e.title.strip():
        return None
    kind = e.kind if e.kind != "other" else (raw.kind_hint or "other")
    sessions = build_sessions(e, kind, venue_hours=place.opening_hours if place else None,
                              today=today, horizon_days=horizon_days)
    d1, d2 = parse_date(e.date_start), parse_date(e.date_end)
    if d1 and d2 and d2 < d1:
        d1, d2 = d2, d1
    if not sessions:
        return None  # no dates at all, or everything is in the past / beyond the horizon
    if not d1 and not d2 and not e.sessions:
        # a permanent exhibition known only by its schedule: no start/end date to claim
        starts_on = ends_on = None
    else:
        starts_on = d1 or sessions[0].starts_at.astimezone(settings.TZ).date()
        ends_on = d2 or (sessions[-1].starts_at.astimezone(settings.TZ).date() if len(sessions) > 1 else None)

    venue_name = e.venue_name or raw.venue_hint
    venue_key = place.id if place else (match_key(venue_name) or "-")
    is_free = e.is_free if e.is_free is not None else raw.is_free_hint
    lat = place.lat if place else e.lat
    lon = place.lon if place else e.lon
    has_time = any(s.time_known for s in sessions)
    seen_at = raw.fetched_at.astimezone(timezone.utc).isoformat()

    return EventRecord(
        id=make_id(e.title, venue_key),
        title=e.title.strip(),
        description=e.description,
        kind=kind,
        is_temporary=(kind in TEMPORARY_KINDS and d2 is not None) or bool(d1 and d2 and (d2 - d1).days >= 2),
        place_id=place.id if place else None,
        venue_name=venue_name,
        address=e.address,
        lat=lat,
        lon=lon,
        starts_on=starts_on,
        ends_on=ends_on,
        schedule_text=e.schedule_opening_hours,
        price_text=e.price_text,
        price_min=e.price_min,
        is_free=is_free,
        age_limit=e.age_limit,
        url=e.url or raw.source_url,
        image_url=e.image_url,
        status=e.status,
        extraction=raw.extraction,
        evidence=e.evidence,
        evidence_ok=raw.evidence_ok or raw.extraction == "jsonld",
        confidence=confidence(raw, has_time),
        sources=[{"source_id": raw.source_id, "url": raw.source_url, "extraction": raw.extraction,
                  "seen_at": seen_at}],
        sessions=sessions,
        match_method=match_method,
    )


def merge(a: EventRecord, b: EventRecord, keep_id: str | None = None) -> EventRecord:
    """Same event from two sources: keep the more reliable one, fill gaps, union sessions and sources."""
    base, other = (a, b) if a.confidence >= b.confidence else (b, a)
    data = base.model_dump()
    for field, val in other.model_dump().items():
        if data.get(field) in (None, "", []) and val not in (None, "", []):
            data[field] = val
    sess = {s.starts_at: s for s in other.sessions}
    sess.update({s.starts_at: s for s in base.sessions})
    data["sessions"] = sorted(sess.values(), key=lambda s: s.starts_at)
    srcs = {(s["source_id"], s["url"]): s for s in a.sources + b.sources}
    data["sources"] = list(srcs.values())
    n = len({urlsplit(s["url"]).netloc for s in data["sources"]})  # independent sites, not listing pages
    data["confidence"] = round(min(0.98, max(a.confidence, b.confidence) + 0.08 * (n - 1)), 3)
    if "cancelled" in (a.status, b.status):  # a cancellation from any source wins
        data["status"] = "cancelled"
    data["id"] = keep_id or base.id
    return EventRecord.model_validate(data)


def _overlap(a: EventRecord, b: EventRecord) -> bool:
    a1, a2 = a.starts_on, a.ends_on or a.starts_on
    b1, b2 = b.starts_on, b.ends_on or b.starts_on
    if not (a1 and b1):
        return False
    return a1 <= b2 and b1 <= a2


def build_records(raws: list[RawEvent], places: list[PlaceLite], today: date | None = None,
                  horizon_days: int = settings.EVENTS_HORIZON_DAYS) -> list[EventRecord]:
    today = today or datetime.now(settings.TZ).date()
    matcher = VenueMatcher(places)
    by_id: dict[str, EventRecord] = {}
    for raw in raws:
        e = raw.event
        m = matcher.match(e.venue_name or raw.venue_hint, e.address, kind=e.kind)
        rec = to_record(raw, m.place if m else None, m.method if m else None, today, horizon_days)
        if rec is None:
            continue
        by_id[rec.id] = merge(by_id[rec.id], rec) if rec.id in by_id else rec

    # Second pass: same title, overlapping dates, one with a known venue and one without -> same event.
    by_title: dict[str, list[EventRecord]] = {}
    for r in by_id.values():
        by_title.setdefault(title_key(r.title), []).append(r)
    final: list[EventRecord] = []
    for group in by_title.values():
        located = [r for r in group if r.place_id]
        unlocated = [r for r in group if not r.place_id]
        keep_unlocated = []
        for u in unlocated:
            if len(located) == 1 and _overlap(located[0], u):
                located[0] = merge(located[0], u, keep_id=located[0].id)
            else:
                keep_unlocated.append(u)
        final += located + keep_unlocated
    return final
