"""'Пульс Астаны': what the city is asking right now, for the big screen.

Privacy by construction: only a ~1 km geohash cell (precision 6), the needs that were understood
(chips, never the text), the language and the number of answers. Kept in memory for 3 hours and
never written anywhere. The same answer store keeps the last answers for a while so the bot and the
Mini App can refer to them (chip edits, "почему не другое", share cards) — also in memory only.
"""
from __future__ import annotations

import threading
import time
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass, field

from .assistant.privacy import cell_center, geohash

WINDOW_S = 3 * 3600


@dataclass
class PulseEvent:
    ts: float
    cell: str
    needs: list[str]
    lang: str
    items: int
    surprise: bool = False
    feedback: dict[str, int] = field(default_factory=dict)
    unmet: list[str] = field(default_factory=list)   # needs no answer covered: "ищут и не находят"


class Pulse:
    def __init__(self, maxlen: int = 20000) -> None:
        self.events: deque[PulseEvent] = deque(maxlen=maxlen)
        self.votes = Counter()
        self.lock = threading.Lock()

    def record(self, lat: float, lon: float, needs: list[str], lang: str, items: int, surprise: bool = False,
               now: float | None = None, unmet: list[str] | None = None) -> None:
        ev = PulseEvent(ts=now or time.time(), cell=geohash(lat, lon, 6), needs=list(needs)[:4], lang=lang,
                        items=items, surprise=surprise, unmet=list(unmet or [])[:4])
        with self.lock:
            self.events.append(ev)

    def vote(self, up: bool) -> None:
        with self.lock:
            self.votes["up" if up else "down"] += 1

    def unmet_live(self, now: float | None = None) -> list[dict]:
        """The last hours, before the daily counts reach DEMAND_K: needs that found nothing, by cell."""
        now = now or time.time()
        with self.lock:
            live = [e for e in self.events if now - e.ts <= WINDOW_S and e.unmet]
        by: dict[tuple[str, str], int] = Counter()
        for e in live:
            for n in e.unmet:
                by[(e.cell, n)] += 1
        out = []
        for (cell, need), k in by.most_common(30):
            lat, lon = cell_center(cell)
            out.append({"cell": cell, "lat": round(lat, 4), "lon": round(lon, 4), "need": need, "n": k})
        return out

    def snapshot(self, now: float | None = None, recent: int = 12) -> dict:
        now = now or time.time()
        with self.lock:
            live = [e for e in self.events if now - e.ts <= WINDOW_S]
            votes = dict(self.votes)
        cells: dict[str, list[PulseEvent]] = {}
        for e in live:
            cells.setdefault(e.cell, []).append(e)
        out_cells = []
        for cell, evs in cells.items():
            lat, lon = cell_center(cell)
            needs = Counter(n for e in evs for n in e.needs)
            out_cells.append({"cell": cell, "lat": round(lat, 4), "lon": round(lon, 4), "n": len(evs),
                              "last_s": int(now - max(e.ts for e in evs)),
                              "top_needs": [n for n, _ in needs.most_common(3)]})
        needs_all = Counter(n for e in live for n in e.needs)
        langs = Counter(e.lang for e in live)
        return {
            "window_min": WINDOW_S // 60,
            "requests": len(live),
            "cells": sorted(out_cells, key=lambda c: -c["n"]),
            "top_needs": [{"need": n, "n": k} for n, k in needs_all.most_common(8)],
            "langs": dict(langs),
            "recent": [{"ago_s": int(now - e.ts), "needs": e.needs, "lang": e.lang, "items": e.items,
                        "surprise": e.surprise, "unmet": e.unmet} for e in reversed(live[-recent:])],
            "unmet_needs": [{"need": n, "n": k} for n, k in Counter(n for e in live for n in e.unmet).most_common(6)],
            "votes": votes,
        }


class AnswerStore:
    """Last answers by id (in memory, bounded) — for chip edits, 'почему не другое' and share cards."""

    def __init__(self, maxlen: int = 2000, ttl_s: int = 6 * 3600) -> None:
        self.data: OrderedDict[str, tuple[float, dict]] = OrderedDict()
        self.maxlen, self.ttl_s = maxlen, ttl_s
        self.lock = threading.Lock()

    def put(self, key: str, value: dict) -> None:
        with self.lock:
            self.data[key] = (time.time(), value)
            self.data.move_to_end(key)
            while len(self.data) > self.maxlen:
                self.data.popitem(last=False)

    def get(self, key: str) -> dict | None:
        with self.lock:
            got = self.data.get(key)
        if got is None or time.time() - got[0] > self.ttl_s:
            return None
        return got[1]


PULSE = Pulse()
ANSWERS = AnswerStore()
