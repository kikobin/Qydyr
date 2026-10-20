"""How the bot reaches the assistant: in the same process (shared database and answer store — the
default, `python -m qydyr_data serve`) or over HTTP when the API runs elsewhere (QYDYR_API_URL)."""
from __future__ import annotations

from typing import Any, Protocol

import httpx

from .. import settings


class Backend(Protocol):
    def ask(self, **body: Any) -> dict: ...
    def patch(self, answer_id: str, patch: dict) -> dict | None: ...
    def answer(self, answer_id: str) -> dict | None: ...
    def walks(self, lat: float, lon: float, lang: str = "ru") -> list[dict]: ...
    def whereami(self, image: bytes, lang: str) -> dict: ...
    def tours(self, lang: str = "ru") -> list[dict]: ...
    def tour(self, tour_id: str, lang: str = "ru") -> dict | None: ...
    def tour_progress(self, tour_id: str, lat: float, lon: float, visited: list[str], accuracy: float | None = None) -> dict: ...
    def tour_story(self, tour_id: str, stop_id: str, lang: str = "ru") -> dict | None: ...
    def walk_nudges(self, lat: float, lon: float, next_lat: float | None, next_lon: float | None, lang: str = "ru") -> dict: ...


class LocalBackend:
    def _data(self):
        from .. import store
        from ..assistant.recommend import DbData
        conn = store.connect()
        return conn, DbData(conn)

    def ask(self, **body: Any) -> dict:
        from .. import api
        conn, data = self._data()
        try:
            return api._run(api.AskIn(**body), data)
        finally:
            conn.close()

    def patch(self, answer_id: str, patch: dict) -> dict | None:
        from fastapi import HTTPException

        from .. import api
        conn, data = self._data()
        try:
            return api.ask_patch(answer_id, api.PatchIn(patch=patch), data)
        except HTTPException:
            return None
        finally:
            conn.close()

    def answer(self, answer_id: str) -> dict | None:
        from ..pulse import ANSWERS
        got = ANSWERS.get(answer_id)
        return got["answer"] if got else None

    def walks(self, lat: float, lon: float, lang: str = "ru") -> list[dict]:
        from .. import api
        conn, data = self._data()
        try:
            return api.walks(lat, lon, None, None, lang=lang, data=data)
        finally:
            conn.close()

    def whereami(self, image: bytes, lang: str) -> dict:
        from .. import api
        conn, data = self._data()
        try:
            return api.run_whereami(image, data, lang=lang)
        finally:
            conn.close()

    def tours(self, lang: str = "ru") -> list[dict]:
        from .. import api
        return api.tours_list(lang)

    def tour(self, tour_id: str, lang: str = "ru") -> dict | None:
        from fastapi import HTTPException

        from .. import api
        try:
            return api.tour_get(tour_id, lang)
        except HTTPException:
            return None

    def tour_progress(self, tour_id: str, lat: float, lon: float, visited: list[str], accuracy: float | None = None) -> dict:
        from .. import api
        return api.tour_progress(tour_id, api.ProgressIn(lat=lat, lon=lon, visited=visited, accuracy=accuracy))

    def tour_story(self, tour_id: str, stop_id: str, lang: str = "ru") -> dict | None:
        from fastapi import HTTPException

        from .. import api
        try:
            return api.tour_story(tour_id, stop_id, lang)
        except HTTPException:
            return None

    def walk_nudges(self, lat: float, lon: float, next_lat: float | None, next_lon: float | None, lang: str = "ru") -> dict:
        from .. import api
        conn, data = self._data()
        try:
            return api.walk_nudges(lat, lon, None, next_lat, next_lon, None, lang, data)
        finally:
            conn.close()


class HttpBackend:
    def __init__(self, base: str) -> None:
        # the whole bot is one client of the API: it carries a key derived from the bot token, so the API
        # does not rate-limit all its users as one IP
        from ..telegram_auth import bot_key
        headers = {"x-qydyr-bot": bot_key(settings.TELEGRAM_BOT_TOKEN)} if settings.TELEGRAM_BOT_TOKEN else {}
        self.c = httpx.Client(base_url=base.rstrip("/"), timeout=40, headers=headers)

    def ask(self, **body: Any) -> dict:
        r = self.c.post("/ask", json=body)
        r.raise_for_status()
        return r.json()

    def patch(self, answer_id: str, patch: dict) -> dict | None:
        r = self.c.post(f"/ask/{answer_id}/patch", json={"patch": patch})
        return r.json() if r.status_code == 200 else None

    def answer(self, answer_id: str) -> dict | None:
        r = self.c.get(f"/answer/{answer_id}")
        return r.json() if r.status_code == 200 else None

    def walks(self, lat: float, lon: float, lang: str = "ru") -> list[dict]:
        r = self.c.get("/walks", params={"lat": lat, "lon": lon, "lang": lang})
        r.raise_for_status()
        return r.json()

    def whereami(self, image: bytes, lang: str) -> dict:
        r = self.c.post("/whereami", files={"photo": ("photo.jpg", image, "image/jpeg")}, data={"lang": lang})
        r.raise_for_status()
        return r.json()

    def tours(self, lang: str = "ru") -> list[dict]:
        r = self.c.get("/tours", params={"lang": lang})
        r.raise_for_status()
        return r.json()

    def tour(self, tour_id: str, lang: str = "ru") -> dict | None:
        r = self.c.get(f"/tours/{tour_id}", params={"lang": lang})
        return r.json() if r.status_code == 200 else None

    def tour_progress(self, tour_id: str, lat: float, lon: float, visited: list[str], accuracy: float | None = None) -> dict:
        r = self.c.post(f"/tours/{tour_id}/progress", json={"lat": lat, "lon": lon, "visited": visited, "accuracy": accuracy})
        r.raise_for_status()
        return r.json()

    def tour_story(self, tour_id: str, stop_id: str, lang: str = "ru") -> dict | None:
        r = self.c.get(f"/tours/{tour_id}/stops/{stop_id}/story", params={"lang": lang})
        return r.json() if r.status_code == 200 else None

    def walk_nudges(self, lat: float, lon: float, next_lat: float | None, next_lon: float | None, lang: str = "ru") -> dict:
        params = {"lat": lat, "lon": lon, "lang": lang}
        if next_lat is not None:
            params.update(next_lat=next_lat, next_lon=next_lon)
        r = self.c.get("/walk/nudges", params=params)
        r.raise_for_status()
        return r.json()


def make_backend() -> Backend:
    return HttpBackend(settings.API_URL) if settings.API_URL else LocalBackend()
