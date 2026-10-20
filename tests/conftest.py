import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from qydyr_data import store
from qydyr_data.events.venues import PlaceLite
from qydyr_data.fetch import FetchError, Page
from qydyr_data.places import overrides
from qydyr_data.places.normalize import normalize_overpass

FIX = Path(__file__).parent / "fixtures"
TEST_DSN = os.getenv("QYDYR_TEST_DSN", "postgresql://postgres:postgres@localhost:5432/qydyr_test")


def fixture_text(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


class FakeFetcher:
    """Serves fixture HTML instead of the network."""

    def __init__(self, pages: dict[str, str], fetched_at: datetime | None = None) -> None:
        self.pages = pages
        self.requested: list[str] = []
        self.fetched_at = fetched_at or datetime(2026, 10, 20, 3, 0, tzinfo=timezone.utc)

    def get(self, url: str, use_cache: bool = True) -> Page:
        self.requested.append(url)
        if url not in self.pages:
            raise FetchError(f"HTTP 404 for {url}")
        return Page(url=url, status=200, text=self.pages[url], fetched_at=self.fetched_at)


@pytest.fixture
def sample_places():
    places = normalize_overpass(json.loads(fixture_text("overpass_sample.json")))
    overrides.apply(places, overrides.load())
    return places


@pytest.fixture
def sample_places_lite(sample_places):
    return [PlaceLite(id=p.id, name=p.name, lat=p.lat, lon=p.lon, category=p.category, name_ru=p.name_ru,
                      name_kk=p.name_kk, name_en=p.name_en, aliases=p.aliases, address=p.address,
                      opening_hours=p.opening_hours, subcategory=p.subcategory)
            for p in sample_places if p.subcategory != "playground"]


_NO_DB: list[str] = []  # remember a failed connect: one timeout per run, not one per test


@pytest.fixture
def db():
    from qydyr_data import settings

    if settings.DATABASE_URL and TEST_DSN == settings.DATABASE_URL:
        pytest.skip("refusing to run destructive tests against DATABASE_URL; set QYDYR_TEST_DSN")
    if _NO_DB:
        pytest.skip(_NO_DB[0])
    try:
        conn = store.connect(TEST_DSN)
    except Exception as e:  # no local Postgres: DB tests are skipped, the rest still runs
        _NO_DB.append(f"no test database ({str(e).splitlines()[0]})")
        pytest.skip(_NO_DB[0])
    conn.execute("drop table if exists event_sessions, events, place_hours, places, sources, demand, city_audit,"
                 " dgis_items, dgis_lookups cascade")
    conn.commit()
    store.init_db(conn)
    yield conn
    conn.close()
