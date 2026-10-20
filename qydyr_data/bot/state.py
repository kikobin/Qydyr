"""In-memory state of the bot: private sessions and group gatherings. Nothing is written to disk;
locations are kept rounded (~150 m) and expire after 30 minutes."""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

LOC_TTL_S = 30 * 60
GATHERING_TTL_S = 6 * 3600


def coarse(lat: float, lon: float) -> tuple[float, float]:
    return round(lat, 3), round(lon, 3)


@dataclass
class Session:
    lang: str = "ru"
    lat: float | None = None
    lon: float | None = None
    loc_at: float = 0.0
    pending: str | None = None          # a request that waited for a location
    walk: "Walk | None" = None          # a guided walk in progress

    def location(self) -> tuple[float, float] | None:
        if self.lat is None or time.time() - self.loc_at > LOC_TTL_S:
            return None
        return self.lat, self.lon  # type: ignore[return-value]

    def set_location(self, lat: float, lon: float) -> None:
        self.lat, self.lon = coarse(lat, lon)
        self.loc_at = time.time()


@dataclass
class Walk:
    """A guided walk in a private chat. Live location updates are used one at a time and dropped: only
    the list of stops already reached is kept."""
    tour_id: str
    title: str
    total: int
    live: bool = False
    visited: list[str] = field(default_factory=list)
    facts: int = 0
    started: float = field(default_factory=time.time)
    last_update: float = 0.0
    last_off_route: float = 0.0
    last_nudge_check: float = 0.0
    nudged: set[str] = field(default_factory=set)


@dataclass
class Member:
    name: str
    wish: str = ""
    lat: float | None = None
    lon: float | None = None


@dataclass
class Gathering:
    gid: str
    chat_id: int
    lang: str = "ru"
    text: str = ""                      # what the starter said: place, time
    message_id: int | None = None       # the bot's "collecting wishes" message
    members: dict[int, Member] = field(default_factory=dict)
    answer: dict | None = None
    poll_id: str | None = None
    options: list[int] = field(default_factory=list)   # item index for each poll option
    votes: dict[int, int] = field(default_factory=dict)
    created: float = field(default_factory=time.time)

    def wishes(self) -> list[str]:
        return [f"{m.name}: {m.wish}" for m in self.members.values() if m.wish]

    def points(self) -> list[tuple[float, float]]:
        return [(m.lat, m.lon) for m in self.members.values() if m.lat is not None]  # type: ignore[misc]


class State:
    def __init__(self) -> None:
        self.sessions: dict[int, Session] = {}
        self.gatherings: dict[str, Gathering] = {}
        self.by_chat: dict[int, str] = {}
        self.by_message: dict[tuple[int, int], str] = {}
        self.by_poll: dict[str, str] = {}

    def session(self, user_id: int) -> Session:
        return self.sessions.setdefault(user_id, Session())

    def new_gathering(self, chat_id: int, lang: str, text: str) -> Gathering:
        self._gc()
        g = Gathering(gid=secrets.token_hex(3), chat_id=chat_id, lang=lang, text=text)
        self.gatherings[g.gid] = g
        self.by_chat[chat_id] = g.gid
        return g

    def bind_message(self, g: Gathering, message_id: int) -> None:
        g.message_id = message_id
        self.by_message[(g.chat_id, message_id)] = g.gid

    def for_reply(self, chat_id: int, message_id: int | None) -> Gathering | None:
        gid = self.by_message.get((chat_id, message_id)) if message_id else None
        return self.gatherings.get(gid) if gid else None

    def current(self, chat_id: int) -> Gathering | None:
        gid = self.by_chat.get(chat_id)
        return self.gatherings.get(gid) if gid else None

    def get(self, gid: str) -> Gathering | None:
        return self.gatherings.get(gid)

    def _gc(self) -> None:
        now = time.time()
        for gid in [k for k, g in self.gatherings.items() if now - g.created > GATHERING_TTL_S]:
            g = self.gatherings.pop(gid)
            self.by_chat.pop(g.chat_id, None)
            self.by_message = {k: v for k, v in self.by_message.items() if v != gid}
            self.by_poll = {k: v for k, v in self.by_poll.items() if v != gid}
