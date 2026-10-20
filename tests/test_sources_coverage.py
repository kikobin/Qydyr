"""More events on the map: sxodim venue quirk, listing pagination, geocoding, planned time, same-name clusters."""
from datetime import date, time

import pytest

from qydyr_data.assistant.intent import parse_rules, parse_start
from qydyr_data.events.crawl import Collector, SourceConfig
from qydyr_data.events.geocode import Geocoder, queries
from qydyr_data.events.jsonld import _real_venue
from qydyr_data.events.venues import PlaceLite, VenueMatcher

from .conftest import FakeFetcher


# ---------- sxodim puts the event title into location.name ----------
def test_venue_from_address_when_location_name_is_the_title():
    title = "Uly Dala Fest"
    addr = "Дворец единоборств имени Ж. Ушкемпирова, пр. Кабанбай батыра, 43, Астана"
    assert _real_venue(title, addr, title) == ("Дворец единоборств имени Ж. Ушкемпирова",
                                               "пр. Кабанбай батыра, 43, Астана")
    # a street first is not a venue
    assert _real_venue(title, "ул. Габдуллина, 18, Астана", title) == (None, "ул. Габдуллина, 18, Астана")
    # a real venue name is kept as is
    assert _real_venue("Бар Pozitiv", "ул. Габдуллина, 18", title) == ("Бар Pozitiv", "ул. Габдуллина, 18")


# ---------- listing pagination ----------
def _listing(*slugs):
    return "".join(f'<a href="https://x.kz/event/{s}">{s}</a>' for s in slugs)


def test_listing_reads_pages_until_nothing_new():
    pages = {
        "https://x.kz/afisha": _listing("a", "b"),
        "https://x.kz/afisha?page=2": _listing("b", "c"),
        "https://x.kz/afisha?page=3": _listing("a", "c"),  # past the end: the site repeats itself
        "https://x.kz/afisha?page=4": _listing("d"),
    }
    fetcher = FakeFetcher(pages)
    src = SourceConfig(id="x", kind="listing", url="https://x.kz/afisha",
                       link_pattern=r"^https://x\.kz/event/[a-z]+$", pages=5)
    Collector(fetcher, None).collect([src])
    events = [u for u in fetcher.requested if "/event/" in u]
    assert events == ["https://x.kz/event/a", "https://x.kz/event/b", "https://x.kz/event/c"]
    assert "https://x.kz/afisha?page=4" not in fetcher.requested


# ---------- geocoding ----------
@pytest.mark.parametrize("address,expected", [
    ("пр. Мангилик Ел, 55 B2.4, Астана", [("Мангилик Ел, 55, Астана", "55")]),
    ("ул. Кабанбай батыра, 5 (здание цирка), Астана", [("Кабанбай батыра, 5, Астана", "5")]),
    ("улица Динмухамеда Кунаева, 14-Д, ЖК «Нурсая-1», Астана", [("Динмухамеда Кунаева, 14д, Астана", "14д")]),
    ("пр. Кабанбай батыра, 53, блок 32, Астана", [("Кабанбай батыра, 53, Астана", "53")]),
    ("ул.Габдуллина 18, Астана", [("Габдуллина, 18, Астана", "18")]),
    ("Кабанбай батыра 53 блок 32, Астана", [("Кабанбай батыра, 53, Астана", "53")]),
    ("пр-т. Мангилик Ел. 10/1, Астана, Астана", [("Мангилик Ел, 10/1, Астана", "10/1")]),
    ("проспект Мангилик Ел, B 2.3, Астана", []),
    ("Астана", []),
])
def test_geocode_queries(address, expected):
    assert queries(address) == expected


def test_geocoder_uses_cache_and_skips_unparseable(tmp_path):
    g = Geocoder(cache_path=tmp_path / "geo.json")
    g.cache = {"Габдуллина, 18, Астана": [51.165, 71.432], "Коргалжын, 3, Астана": None}
    g._search = lambda q, house: pytest.fail("network call despite cache")  # type: ignore[method-assign]
    assert g.locate("ул. Габдуллина, 18, Астана") == (51.165, 71.432)
    assert g.locate("шоссе Коргалжын, 3, Астана") is None
    assert g.locate("Астана") is None and g.locate(None) is None


# ---------- planned time ----------
@pytest.mark.parametrize("text,start,days", [
    ("вечером хочу на стендап", time(19, 0), 0),
    ("завтра в 11:00 в музей", time(11, 0), 1),
    ("в 7 вечера концерт", time(19, 0), 0),
    ("tomorrow evening", time(19, 0), 1),
    ("завтра куда-нибудь", time(12, 0), 1),
    ("есть час до 19:00", None, 0),  # a deadline, not a start
    ("хочу кофе", None, 0),
])
def test_parse_start(text, start, days):
    assert parse_start(text) == (start, days)


@pytest.mark.parametrize("text", ["хочу на стендап или квиз", "куда на шоу", "go to a stand-up", "на балет"])
def test_event_words(text):
    assert "event" in parse_rules(text).needs


def test_showroom_is_not_a_show():
    assert "event" not in parse_rules("шоурум одежды").needs


# ---------- one spot under one name ----------
def test_same_name_cluster_prefers_the_real_place():
    monument = PlaceLite(id="m", name="Бәйтерек", lat=51.12830, lon=71.43050, category="landmark", aliases=["Байтерек"])
    station = PlaceLite(id="s", name="Бәйтерек", lat=51.12700, lon=71.43300, category="anchor", aliases=["Байтерек"])
    m = VenueMatcher([monument, station])
    assert m.match("Байтерек").place.id == "m"
    # one building mapped twice: the outline with an address wins
    hall = PlaceLite(id="w", name="Astana Musical", lat=51.12275, lon=71.44176, category="culture",
                     aliases=["ЦКЗ Казахстан"], address="Мәңгілік Ел даңғылы, 10/1")
    org = PlaceLite(id="n", name="Қазақконцерт", lat=51.12300, lon=71.44187, category="culture",
                    aliases=["ЦКЗ Казахстан"])
    assert VenueMatcher([org, hall]).match("ЦКЗ Казахстан").place.id == "w"
    # two branches of a chain far apart stay ambiguous
    a = PlaceLite(id="a", name="Harat's", lat=51.09, lon=71.41, category="nightlife")
    b = PlaceLite(id="b", name="Harat's", lat=51.17, lon=71.45, category="nightlife")
    assert VenueMatcher([a, b]).match("Harat's") is None


# ---------- a series listed as one date range is not "open all day" ----------
def test_screening_series_does_not_borrow_venue_hours():
    from datetime import date

    from qydyr_data.events.models import ExtractedEvent
    from qydyr_data.events.sessions import build_sessions

    ev = ExtractedEvent(title="Киноужин «Рататуй»", kind="other", date_start="2026-10-18", date_end="2026-11-28")
    got = build_sessions(ev, "other", venue_hours="Mo-Su 14:00-04:00", today=date(2026, 10, 20))
    assert got and all(not s.time_known and s.hours_from == "none" for s in got)
    expo = build_sessions(ev, "exhibition", venue_hours="Mo-Su 10:00-18:00", today=date(2026, 10, 20))
    assert expo[0].hours_from == "venue" and expo[0].starts_at.hour == 10


# ---------- a waiting room means "slow down", not "retry harder" ----------
def test_waiting_room_blocks_the_host(tmp_path):
    import httpx

    from qydyr_data.fetch import FetchError, PoliteFetcher

    hits = []

    def handler(req: httpx.Request) -> httpx.Response:
        hits.append(str(req.url))
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nCrawl-delay: 2\n")
        if req.url.host == "queue.ticketon.kz":
            return httpx.Response(200, text="<html>you are in line</html>")
        return httpx.Response(302, headers={"Location": "https://queue.ticketon.kz/?c=ticketon"})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    f = PoliteFetcher(client=client, cache_dir=tmp_path, delay_s=0)
    with pytest.raises(FetchError, match="waiting room"):
        f.get("https://ticketon.kz/astana/theatres")
    assert f.host_delays["ticketon.kz"] == 2  # robots Crawl-delay is honoured
    n = len(hits)
    with pytest.raises(FetchError, match="rate-limiting"):
        f.get("https://ticketon.kz/theatres/event/x")
    assert len(hits) == n  # no request at all once blocked


# ---------- ticket sites: "first show ever – last show" is not a daily period ----------
_TICKETON_PAGE = """<html><head><title>Волк и 7 козлят</title>
<script type="application/ld+json">{"@context": "https://schema.org", "@type": "ChildrensEvent", "name": "Волк и 7 козлят",
 "url": "https://ticketon.kz/children/event/volk", "startDate": "2026-02-20T18:00:00+05:00",
 "endDate": "2026-10-31T16:45:00+05:00", "location": {"@type": "Place", "name": "Абу-Даби Плаза",
 "address": "Астана, Улица Сыганак, 60/5"}}</script></head>
<body><main><h1>Волк и 7 козлят</h1><p>24 октября, суббота</p><p>Абу-Даби Плаза</p><p>16:00</p></main></body></html>"""


def test_repertory_span_goes_to_the_text():
    from qydyr_data.events.llm import StaticExtractor

    url = "https://ticketon.kz/children/event/volk"
    src = SourceConfig(id="t", kind="page", url=url, mode="page")
    llm = StaticExtractor({url: [{"title": "Волк и 7 козлят", "kind": "kids", "venue_name": "Абу-Даби Плаза",
                                  "sessions": [{"date": "2026-10-24", "time_start": "16:00"}],
                                  "evidence": "24 октября, суббота"}]})
    raws, _ = Collector(FakeFetcher({url: _TICKETON_PAGE}), llm, today=date(2026, 10, 20)).collect([src])
    assert [r.extraction for r in raws] == ["llm"] and raws[0].event.sessions[0].date == "2026-10-24"
    # without Claude the event is dropped rather than shown every day at 18:00
    raws, _ = Collector(FakeFetcher({url: _TICKETON_PAGE}), None, today=date(2026, 10, 20)).collect([src])
    assert raws == []


def test_shared_address_does_not_pick_a_tenant():
    mall = PlaceLite(id="osm:way/7", name="Abu Dhabi Plaza", lat=51.1200, lon=71.4300, category="shopping",
                     subcategory="mall", address="Сығанақ көшесі, 60/5")
    cafe = PlaceLite(id="osm:node/9", name="Farsh", lat=51.1201, lon=71.4301, category="food",
                     subcategory="fast_food", address="Сығанақ көшесі, 60/5")
    m = VenueMatcher([cafe, mall]).match("Абу-Даби Плаза", "Сығанақ көшесі, 60/5", kind="kids")
    assert m is None or m.place.id == "osm:way/7"


@pytest.mark.parametrize("text,wd,start", [
    ("в субботу днём с ребёнком на спектакль", 5, time(14, 0)),
    ("в пятницу вечером", 4, time(19, 0)),
    ("on Sunday", 6, time(12, 0)),
    ("на выходных", 5, time(12, 0)),
    ("хочу кофе", None, None),
])
def test_weekday(text, wd, start):
    it = parse_rules(text)
    assert (it.start_weekday, it.start_time) == (wd, start)


def test_weekday_moves_the_answer_time():
    from datetime import datetime

    from qydyr_data import settings
    from qydyr_data.assistant.recommend import Assistant

    class NoData:
        def places_near(self, *a, **k): return []
        def whats_on(self, *a, **k): return []
        def places_lite(self): return []

    tue = datetime(2026, 10, 20, 9, 30, tzinfo=settings.TZ)
    ans = Assistant(data=NoData(), places=[], llm=None).ask("в субботу днём куда-нибудь", at=tue, lat=51.13, lon=71.43,
                                                             fetch_weather=False)
    assert ans.at == datetime(2026, 10, 24, 14, 0, tzinfo=settings.TZ)
