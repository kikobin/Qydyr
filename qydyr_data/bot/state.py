"""In-memory state of the bot: private sessions and group gatherings. Nothing is written to disk;
locations are kept rounded (~150 m) and expire after 30 minutes."""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

from .texts import user_lang

LOC_TTL_S = 30 * 60
PENDING_TTL_S = 10 * 60             # a request waiting for a location is forgotten after this
GATHERING_TTL_S = 6 * 3600


def coarse(lat: float, lon: float) -> tuple[float, float]:
    return round(lat, 3), round(lon, 3)


@dataclass
class Session:
    lang: str = "ru"
    lang_set: bool = False              # chosen with /lang: /start and language_code no longer override it
    lat: float | None = None
    lon: float | None = None
    loc_at: float = 0.0
    pending: str | None = None          # a request that waited for a location
    pending_at: float = 0.0
    walk: "Walk | None" = None          # a guided walk in progress
    gen_tours: dict[str, dict] = field(default_factory=dict)   # themed walks from the last /walks, as tours

    def location(self) -> tuple[float, float] | None:
        if self.lat is None or time.time() - self.loc_at > LOC_TTL_S:
            return None
        return self.lat, self.lon  # type: ignore[return-value]

    def set_location(self, lat: float, lon: float) -> None:
        self.lat, self.lon = coarse(lat, lon)
        self.loc_at = time.time()

    def clear_location(self) -> None:
        self.lat = self.lon = None
        self.loc_at = 0.0

    def loc_age_min(self) -> int:
        return int((time.time() - self.loc_at) // 60)

    def set_pending(self, text: str | None) -> None:
        self.pending, self.pending_at = text, time.time()

    def take_pending(self) -> str | None:
        p, self.pending = self.pending, None
        return p if p and time.time() - self.pending_at <= PENDING_TTL_S else None

    def peek_pending(self) -> str | None:
        return self.pending if self.pending and time.time() - self.pending_at <= PENDING_TTL_S else None


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
    last_progress: float = 0.0
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
    text: str = ""                      # what the starter said: place, time, wish
    starter: int | None = None
    message_id: int | None = None       # the bot's "collecting wishes" message
    members: dict[int, Member] = field(default_factory=dict)
    answer: dict | None = None
    poll_id: str | None = None
    poll_message_id: int | None = None
    options: list[int] = field(default_factory=list)   # item index for each poll option
    votes: dict[int, int] = field(default_factory=dict)  # votes of the current poll only
    picking: bool = False               # a pick is running: a second tap waits
    final_done: bool = False            # "Итог" already announced for the current poll
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
        self.chat_lang: dict[int, str] = {}         # group chats: language chosen with /lang there
        self.voted: set[tuple[str, int]] = set()    # (answer id, user id): feedback counts once

    def session(self, user_id: int, language_code: str | None = None) -> Session:
        s = self.sessions.get(user_id)
        if s is None:
            s = self.sessions[user_id] = Session(lang=user_lang(language_code))
        return s

    def vote_once(self, answer_id: str, user_id: int) -> bool:
        if len(self.voted) > 20000:
            self.voted.clear()
        key = (answer_id, user_id)
        if key in self.voted:
            return False
        self.voted.add(key)
        return True

    def new_gathering(self, chat_id: int, lang: str, text: str) -> Gathering:
        self._gc()
        g = Gathering(gid=secrets.token_hex(3), chat_id=chat_id, lang=lang, text=text)
        self.gatherings[g.gid] = g
        self.by_chat[chat_id] = g.gid
        return g

    def bind_message(self, g: Gathering, message_id: int) -> None:
        g.message_id = message_id
        self.by_message[(g.chat_id, message_id)] = g.gid

    def bind_prompt(self, g: Gathering, message_id: int) -> None:
        """A "Где вы?" asked after «Подобрать»: a reply to it counts as a reply to the gathering."""
        self.by_message[(g.chat_id, message_id)] = g.gid

    def reset_poll(self, g: Gathering) -> None:
        """A new answer for the gathering: the old poll stops counting, votes start from zero."""
        if g.poll_id:
            self.by_poll.pop(g.poll_id, None)
        g.poll_id = g.poll_message_id = None
        g.votes, g.final_done = {}, False

    def bind_poll(self, g: Gathering, poll_id: str, message_id: int) -> None:
        g.poll_id, g.poll_message_id = poll_id, message_id
        self.by_poll[poll_id] = g.gid

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
            if self.by_chat.get(g.chat_id) == gid:
                self.by_chat.pop(g.chat_id, None)
            self.by_message = {k: v for k, v in self.by_message.items() if v != gid}
            self.by_poll = {k: v for k, v in self.by_poll.items() if v != gid}
