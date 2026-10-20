"""The location a person shared with the bot, for the Mini App they open from it: Telegram's webview often has
no geolocation of its own. Coarse (~150 m, rounded by the bot), kept in memory until the next one (a day at most), keyed by the
Telegram user id that the Mini App proves with signed initData. Nothing is written anywhere."""
from __future__ import annotations

import time

TTL_S = 24 * 3600   # as long as the bot keeps it: until the next one
_LAST: dict[int, tuple[float, float, float]] = {}


def remember(user_id: int, lat: float, lon: float) -> None:
    _LAST[user_id] = (lat, lon, time.time())
    if len(_LAST) > 10_000:                      # a long-running server: drop the stale ones
        now = time.time()
        for k in [k for k, v in _LAST.items() if now - v[2] > TTL_S]:
            del _LAST[k]


def recall(user_id: int | None) -> tuple[float, float] | None:
    v = _LAST.get(user_id) if user_id else None
    if v is None or time.time() - v[2] > TTL_S:
        return None
    return v[0], v[1]
