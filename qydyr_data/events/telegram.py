"""Public Telegram channels via the t.me/s/<channel> web preview (no login, no bot token).

Only public channels; we read posts, never users. Each post goes to the LLM in "post" mode with the
post date as the reference date, so «сегодня в 19:00» resolves correctly.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from bs4 import BeautifulSoup
from dateutil import parser as dtparser

EVENT_HINT_RE = re.compile(
    r"(выставк|концерт|спектакл|фестивал|мастер-?класс|лекци|вечеринк|стендап|stand-?up|кинопоказ|"
    r"экскурси|ярмарк|маркет|открыти|премьер|квиз|турнир|билет|вход свободный|вход бесплатный|"
    r"регистрац|көрме|концерті|қойылым|фестиваль|\d{1,2}[:.]\d{2}|\d{1,2}\s+(январ|феврал|март|апрел|ма[йя]|"
    r"июн|июл|август|сентябр|октябр|ноябр|декабр|қаңтар|ақпан|наурыз|сәуір|мамыр|маусым|шілде|тамыз|"
    r"қыркүйек|қазан|қараша|желтоқсан))",
    re.I,
)


@dataclass
class TgPost:
    channel: str
    post_id: str
    url: str
    published_at: datetime
    text: str


def channel_url(channel: str) -> str:
    return f"https://t.me/s/{channel.lstrip('@').strip('/')}"


def parse_channel_page(html: str) -> list[TgPost]:
    soup = BeautifulSoup(html, "lxml")
    posts: list[TgPost] = []
    for msg in soup.select("div.tgme_widget_message[data-post]"):
        data_post = msg["data-post"]  # "channel/123"
        channel, _, pid = data_post.partition("/")
        text_el = msg.select_one(".tgme_widget_message_text")
        text = text_el.get_text("\n", strip=True) if text_el else ""
        t = msg.select_one("a.tgme_widget_message_date time[datetime]") or msg.select_one("time[datetime]")
        if not t:
            continue
        published = dtparser.isoparse(t["datetime"])
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        posts.append(TgPost(channel=channel, post_id=pid, url=f"https://t.me/{data_post}",
                            published_at=published, text=text))
    return posts


def looks_like_event(text: str) -> bool:
    return bool(text) and EVENT_HINT_RE.search(text) is not None


def fresh_event_posts(posts: list[TgPost], max_age_days: int = 14, now: datetime | None = None) -> list[TgPost]:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max_age_days)
    return [p for p in posts if p.published_at >= cutoff and looks_like_event(p.text)]
