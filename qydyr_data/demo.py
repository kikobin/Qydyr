"""Demo data: the test fixtures (21 places around the museum, EXPO and Mega Silk Way, an afisha with an
evening exhibition, a temporary exhibition, a cancelled concert and a Telegram post about a jazz
evening) loaded into the database through the real pipeline — for rehearsals, video recording and
laptops without internet. Pair it with demo mode:

    python -m qydyr_data demo-data
    QYDYR_DEMO_NOW=2026-10-23T19:10 QYDYR_WEATHER_FILE=tests/fixtures/weather_mild.json python -m qydyr_data serve
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

from . import settings, store
from .events.build import build_records
from .events.crawl import Collector, SourceConfig
from .events.llm import StaticExtractor
from .fetch import FetchError, Page
from .places import overrides
from .places.normalize import normalize_overpass

FIX = settings.ROOT / "tests" / "fixtures"
DEMO_DAY = date(2026, 10, 23)
EVENT_RE = r"^https://sxodim\.com/astana/event/[a-z0-9-]+/?$"

PAGES = {
    "https://sxodim.com/astana/afisha": "listing.html",
    "https://sxodim.com/astana/event/vystavka-svet-velikoy-stepi": "event_jsonld.html",
    "https://sxodim.com/astana/event/noch-v-muzee": "event_page_text.html",
    "https://sxodim.com/astana/event/koncert-osennie-melodii": "event_cancelled_jsonld.html",
    "https://t.me/s/test_afisha_channel": "telegram_channel.html",
}
LLM_OUTPUTS = {  # what Claude returns for the pages without JSON-LD
    "https://sxodim.com/astana/event/noch-v-muzee": [{
        "title": "Вечерняя выставка «Ночь в музее»", "kind": "exhibition",
        "venue_name": "Национальный музей Республики Казахстан", "address": "пр. Тәуелсіздік, 54",
        "date_start": "2026-10-23", "time_start": "18:00", "time_end": "22:00", "is_free": True,
        "evidence": "Когда: 23 октября, с 18:00 до 22:00",
        "description": "Залы золота открыты вечером: экскурсии каждые полчаса, живая домбра."}],
    "https://t.me/test_afisha_channel/101": [{
        "title": "Джаз-вечер", "kind": "concert", "venue_name": "бар «Луна»", "date_start": "2026-10-23",
        "time_start": "20:00", "price_text": "3000 тг", "price_min": 3000,
        "evidence": "Сегодня в 20:00 джаз-вечер в баре «Луна» на EXPO."}],
}
SOURCES = [
    SourceConfig(id="sxodim_afisha", kind="listing", url="https://sxodim.com/astana/afisha", link_pattern=EVENT_RE),
    SourceConfig(id="telegram_afisha", kind="telegram", channels=["test_afisha_channel"], max_age_days=10,
                 reliability=0.65),
]


class FixtureFetcher:
    """Serves saved HTML instead of the network."""

    def __init__(self, pages: dict[str, str], fetched_at: datetime | None = None) -> None:
        self.pages = pages
        self.requested: list[str] = []
        self.fetched_at = fetched_at or datetime(2026, 10, 20, 3, 0, tzinfo=timezone.utc)

    def get(self, url: str, use_cache: bool = True) -> Page:
        self.requested.append(url)
        if url not in self.pages:
            raise FetchError(f"HTTP 404 for {url}")
        return Page(url=url, status=200, text=self.pages[url], fetched_at=self.fetched_at)


def demo_places():
    places = normalize_overpass(json.loads((FIX / "overpass_sample.json").read_text(encoding="utf-8")))
    overrides.apply(places, overrides.load())
    return places


def load(conn, places=None, pages: dict[str, str] | None = None):
    """Places + events into the database. Returns (raws, records, stats, extractor)."""
    places = places if places is not None else demo_places()
    store.upsert_places(conn, places, now=datetime(2026, 10, 20, 8, tzinfo=settings.TZ))
    pages = pages or {u: (FIX / f).read_text(encoding="utf-8") for u, f in PAGES.items()}
    extractor = StaticExtractor(LLM_OUTPUTS)
    collector = Collector(FixtureFetcher(pages), extractor, today=DEMO_DAY)
    raws, stats = collector.collect(SOURCES)
    records = build_records(raws, store.load_places_lite(conn), today=collector.today)
    store.upsert_events(conn, records)
    return raws, records, stats, extractor
