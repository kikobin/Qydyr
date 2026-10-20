"""Deterministic Russian / Kazakh date and time parsing.

Used to double-check what the LLM extracted: dates found in the evidence quote must agree
with the extracted dates, otherwise the event loses confidence.
"""
from __future__ import annotations

import re
from datetime import date, time, timedelta

_MONTH_PREFIXES: list[tuple[str, int]] = [
    ("январ", 1), ("янв", 1), ("қаңтар", 1),
    ("феврал", 2), ("фев", 2), ("ақпан", 2),
    ("март", 3), ("мар", 3), ("наурыз", 3),
    ("апрел", 4), ("апр", 4), ("сәуір", 4),
    ("мая", 5), ("май", 5), ("мае", 5), ("мамыр", 5),
    ("июн", 6), ("маусым", 6),
    ("июл", 7), ("шілде", 7),
    ("август", 8), ("авг", 8), ("тамыз", 8),
    ("сентябр", 9), ("сент", 9), ("сен", 9), ("қыркүйек", 9),
    ("октябр", 10), ("окт", 10), ("қазан", 10),
    ("ноябр", 11), ("нояб", 11), ("ноя", 11), ("қараша", 11),
    ("декабр", 12), ("дек", 12), ("желтоқсан", 12),
]
_MONTH_ALT = "|".join(sorted({re.escape(p) for p, _ in _MONTH_PREFIXES}, key=len, reverse=True))
MONTH = rf"(?:{_MONTH_ALT})[а-яәғқңөұүһі]*\.?"

_DASH = r"(?:-|–|—|по|до)"
_RANGE_RE = re.compile(
    rf"(?:\bс\s+)?(?P<d1>\d{{1,2}})(?:\s*(?P<m1>{MONTH}))?(?:\s*(?P<y1>\d{{4}}))?"
    rf"\s*{_DASH}\s*(?P<d2>\d{{1,2}})\s*(?P<m2>{MONTH})(?:\s*(?P<y2>\d{{4}}))?",
    re.IGNORECASE,
)
_UNTIL_RE = re.compile(rf"\bдо\s+(?P<d>\d{{1,2}})\s*(?P<m>{MONTH})(?:\s*(?P<y>\d{{4}}))?", re.IGNORECASE)
_SINGLE_RE = re.compile(
    rf"\b(?P<d>\d{{1,2}})(?:-?(?:го|е|ое|і|ы|сы))?\s*-?\s*(?P<m>{MONTH})(?:\s*(?P<y>\d{{4}}))?",
    re.IGNORECASE,
)
_NUMERIC_RE = re.compile(r"\b(?P<d>\d{1,2})\.(?P<m>\d{1,2})\.(?P<y>\d{4}|\d{2})\b")
_ISO_RE = re.compile(r"\b(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})\b")
_TIME_RE = re.compile(r"(?<![\d.:])([01]?\d|2[0-3])[:.]([0-5]\d)(?![\d.:])")


def month_num(word: str | None) -> int | None:
    if not word:
        return None
    w = word.lower().strip(". ")
    for prefix, num in sorted(_MONTH_PREFIXES, key=lambda x: len(x[0]), reverse=True):
        if w.startswith(prefix):
            return num
    return None


def _mk(day: int, month: int, year: int | None, ref: date) -> date | None:
    try:
        if year:
            return date(year if year > 99 else 2000 + year, month, day)
        d = date(ref.year, month, day)
    except ValueError:
        return None
    if d < ref - timedelta(days=60):
        try:
            d = date(ref.year + 1, month, day)
        except ValueError:
            return None
    elif d > ref + timedelta(days=300):
        try:
            d = date(ref.year - 1, month, day)
        except ValueError:
            return None
    return d


def _blank(text: str, m: re.Match) -> str:
    return text[: m.start()] + " " * (m.end() - m.start()) + text[m.end():]


def find_ranges(text: str, ref: date) -> list[tuple[date | None, date | None]]:
    t = text.lower()
    out: list[tuple[date | None, date | None]] = []
    for m in _RANGE_RE.finditer(t):
        m2 = month_num(m["m2"])
        m1 = month_num(m["m1"]) or m2
        y2 = int(m["y2"]) if m["y2"] else None
        y1 = int(m["y1"]) if m["y1"] else None
        end = _mk(int(m["d2"]), m2, y2, ref)
        start: date | None
        if end and not y1:
            # the start belongs to the end's year, or the year before ("20 декабря – 10 января")
            try:
                start = date(end.year, m1, int(m["d1"]))
                if start > end:
                    start = date(end.year - 1, m1, int(m["d1"]))
            except ValueError:
                start = None
        else:
            start = _mk(int(m["d1"]), m1, y1, ref)
        if start or end:
            out.append((start, end))
    t2 = _RANGE_RE.sub(lambda m: " " * (m.end() - m.start()), t)
    for m in _UNTIL_RE.finditer(t2):
        end = _mk(int(m["d"]), month_num(m["m"]), int(m["y"]) if m["y"] else None, ref)
        if end:
            out.append((None, end))
    return out


def find_dates(text: str, ref: date) -> list[date]:
    """All concrete dates mentioned (range endpoints included)."""
    t = text.lower()
    found: list[date] = []
    for a, b in find_ranges(t, ref):
        found += [x for x in (a, b) if x]
    t = _RANGE_RE.sub(lambda m: " " * (m.end() - m.start()), t)
    t = _UNTIL_RE.sub(lambda m: " " * (m.end() - m.start()), t)
    for m in _SINGLE_RE.finditer(t):
        d = _mk(int(m["d"]), month_num(m["m"]), int(m["y"]) if m["y"] else None, ref)
        if d:
            found.append(d)
    for rx in (_NUMERIC_RE, _ISO_RE):
        for m in rx.finditer(t):
            try:
                y = int(m["y"])
                found.append(date(y if y > 99 else 2000 + y, int(m["m"]), int(m["d"])))
            except ValueError:
                pass
    return sorted(set(found))


def find_times(text: str) -> list[time]:
    t = _NUMERIC_RE.sub(" ", text)
    t = _ISO_RE.sub(" ", t)
    out = []
    for h, mnt in _TIME_RE.findall(t):
        out.append(time(int(h), int(mnt)))
    return out


def dates_consistent(event_dates: list[date], evidence: str | None, ref: date) -> bool | None:
    """True/False if the evidence mentions dates; None when it has nothing to compare."""
    wanted = [d for d in event_dates if d]
    if not evidence or not wanted:
        return None
    singles = set(find_dates(evidence, ref))
    ranges = find_ranges(evidence, ref)
    if not singles and not ranges:
        return None

    def covered(d: date) -> bool:
        if d in singles:
            return True
        return any((a is None or a <= d) and (b is None or d <= b) for a, b in ranges)

    return all(covered(d) for d in wanted)
