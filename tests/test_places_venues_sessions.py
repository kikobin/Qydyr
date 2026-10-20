from datetime import date, datetime

from qydyr_data import settings
from qydyr_data.events.models import ExtractedEvent, LLMSession
from qydyr_data.events.sessions import build_sessions
from qydyr_data.events.venues import VenueMatcher

TZ = settings.TZ
TODAY = date(2026, 10, 20)  # Tuesday
MUSEUM = "Tu-Su 10:00-18:00; Tu[-1] off"


# ---------- places ----------
def test_overpass_normalization(sample_places):
    by_id = {p.id: p for p in sample_places}
    assert "osm:node/1011" not in by_id  # ATM: not a POI for walks
    assert "osm:node/1012" not in by_id  # unnamed cafe
    museum = by_id["osm:node/1001"]
    assert (museum.category, museum.subcategory, museum.indoor) == ("culture", "museum", "indoor")
    assert museum.hours_valid and museum.address == "проспект Тәуелсіздік, 54"
    assert by_id["osm:node/1006"].name == "Детская площадка"
    assert by_id["osm:way/2002"].indoor == "indoor"                     # theatre
    assert (by_id["osm:way/2002"].lat, by_id["osm:way/2002"].lon) == (51.1307, 71.4146)  # way center
    assert by_id["osm:relation/3013"].category == "landmark"
    assert by_id["osm:node/1005"].attrs.get("wifi") is True
    assert not by_id["osm:node/1014"].hours_valid                      # broken opening_hours kept as invalid


def test_overrides_add_people_names(sample_places):
    baiterek = next(p for p in sample_places if p.id == "osm:node/1003")
    assert "Чупа-чупс" in baiterek.aliases
    assert baiterek.indoor == "mixed"


# ---------- venue matching ----------
def test_venue_matching(sample_places_lite):
    m = VenueMatcher(sample_places_lite)

    def pid(venue, address=None, kind=None):
        r = m.match(venue, address, kind)
        return r.place.id if r else None

    assert pid("Национальный музей РК") == "osm:node/1001"
    assert pid("Нацмузей") == "osm:node/1001"
    assert pid(None, "пр. Тауельсиздик, 54") == "osm:node/1001"      # address only, other spelling
    assert pid("Астана Опера") == "osm:way/2002"
    assert pid("ГТОБ «Астана Опера»") == "osm:way/2002"
    assert pid("Чупа-чупс") == "osm:node/1003"
    assert pid("Хан-Шатыр") == pid("Khan Shatyr") == "osm:way/2004"
    assert pid("бар «Луна»", kind="concert") == "osm:node/1007"      # fuzzy
    assert pid("Музей") is None                                       # two museums: refuse to guess
    assert pid("Какое-то неизвестное место") is None


# ---------- sessions ----------
def test_exhibition_period_uses_venue_hours():
    ev = ExtractedEvent(title="Свет Великой степи", kind="exhibition", date_start="2026-10-01", date_end="2026-10-26")
    s = build_sessions(ev, "exhibition", venue_hours=MUSEUM, today=TODAY)
    days = [x.starts_at.date() for x in s]
    assert days == [date(2026, 10, d) for d in (20, 21, 22, 23, 24, 25)]  # Mon 26th is closed
    assert all(x.hours_from == "venue" and x.time_known for x in s)
    assert s[0].starts_at == datetime(2026, 10, 20, 10, tzinfo=TZ)


def test_event_schedule_beats_venue_and_skips_sanitary_day():
    ev = ExtractedEvent(title="X", kind="exhibition", date_start="2026-10-20", date_end="2026-10-31",
                        schedule_opening_hours=MUSEUM)
    s = build_sessions(ev, "exhibition", venue_hours="Mo-Su 09:00-21:00", today=TODAY)
    days = {x.starts_at.date() for x in s}
    assert date(2026, 10, 27) not in days and date(2026, 10, 26) not in days
    assert all(x.hours_from == "event" for x in s)


def test_single_concert_and_overnight_party_and_date_only():
    c = build_sessions(ExtractedEvent(title="c", kind="concert", date_start="2026-10-23", time_start="19:00"),
                       "concert", venue_hours=None, today=TODAY)[0]
    assert c.end_estimated and c.ends_at == datetime(2026, 10, 23, 21, tzinfo=TZ)
    p = build_sessions(ExtractedEvent(title="p", kind="party", date_start="2026-10-24", time_start="22:00",
                                      time_end="02:00"), "party", venue_hours=None, today=TODAY)[0]
    assert p.ends_at == datetime(2026, 10, 25, 2, tzinfo=TZ) and not p.end_estimated
    d = build_sessions(ExtractedEvent(title="d", kind="fair", date_start="2026-10-24"), "fair",
                       venue_hours=None, today=TODAY)[0]
    assert not d.time_known and d.hours_from == "none"


def test_explicit_sessions_and_past_dates_dropped():
    ev = ExtractedEvent(title="Абай", kind="performance", sessions=[
        LLMSession(date="2026-10-10", time_start="19:00"),  # past
        LLMSession(date="2026-10-23", time_start="19:00"),
        LLMSession(date="2026-10-24", time_start="18:00", time_end="20:30"),
    ])
    s = build_sessions(ev, "performance", venue_hours=None, today=TODAY)
    assert [x.starts_at for x in s] == [datetime(2026, 10, 23, 19, tzinfo=TZ), datetime(2026, 10, 24, 18, tzinfo=TZ)]
