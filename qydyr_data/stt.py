"""Speech to text for voice requests (the Mini App's mic and voice messages to the bot). Claude takes no audio,
and the browser's own recognition does not exist in Telegram's webview, so the audio goes to Whisper on Groq
(an OpenAI-compatible endpoint, free tier). Nothing is kept: the audio is sent once and dropped."""
from __future__ import annotations

import logging

import httpx

from . import settings

log = logging.getLogger(__name__)
MAX_BYTES = 4 * 1024 * 1024        # ~1-2 minutes of opus: a request, not a podcast
PROMPT = {   # spelling hints: the city's names come out right instead of "Хан шатер", "Байтерик"
    "ru": "Астана, Хан Шатыр, Байтерек, Mega Silk Way, Керуен, Экспо, Есиль, кофе, с ребёнком, на час.",
    "kk": "Астана, Хан Шатыр, Бәйтерек, Mega Silk Way, Керуен, Экспо, Есіл, кофе, баламен, бір сағат.",
    "en": "Astana, Khan Shatyr, Baiterek, Mega Silk Way, Keruen, Expo, Yesil, coffee, with kids, for an hour.",
}


def available() -> bool:
    return bool(settings.STT_API_KEY)


def transcribe(audio: bytes, filename: str = "voice.ogg", lang: str = "ru") -> str | None:
    """The words, or None when there is no key, nothing was said, or the service failed."""
    if not settings.STT_API_KEY or not audio or len(audio) > MAX_BYTES:
        return None
    try:
        r = httpx.post(f"{settings.STT_URL.rstrip('/')}/audio/transcriptions",
                       headers={"Authorization": f"Bearer {settings.STT_API_KEY}"},
                       files={"file": (filename, audio)},
                       data={"model": settings.STT_MODEL, "language": lang if lang in PROMPT else "ru",
                             "prompt": PROMPT.get(lang, PROMPT["ru"]), "response_format": "json", "temperature": "0"},
                       timeout=30)
        r.raise_for_status()
        text = (r.json().get("text") or "").strip()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("speech to text failed: %s", e.__class__.__name__)
        return None
    return text or None
