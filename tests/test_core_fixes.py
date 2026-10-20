"""Regressions from the review on real Astana data: where the person is, what they asked for, what is
open, one plan on the screen, and the data pipeline. The fixture city gets the real pitfalls on top
(tests/fixtures/overpass_city_extra.json)."""
import json
from datetime import date, datetime
from types import SimpleNamespace

import httpx
import pytest

from qydyr_data import settings
from qydyr_data.assistant import air as A
from qydyr_data.assistant import knowledge as K
from qydyr_data.assistant import travel
from qydyr_data.assistant import weather as W
from qydyr_data.assistant.intent import event_kind_fits, parse_budget, parse_rules, understand
from qydyr_data.assistant.locate import Locator
from qydyr_data.assistant.recommend import Assistant, DbData
from qydyr_data.events.models import ExtractedEvent
from qydyr_data.events.sessions import build_sessions
from qydyr_data.events.venues import PlaceLite, VenueMatcher
from qydyr_data.places import overrides
from qydyr_data.places.normalize import normalize_overpass

from .conftest import FIX, fixture_text
from .test_pipeline_db import load_everything

TZ = settings.TZ
MILD = FIX / "weather_mild.json"
BAITEREK, EXPO = (51.1283, 71.4305), (51.0900, 71.4165)


def at(h, m=0, day=23):
    return datetime(2026, 10, day, h, m, tzinfo=TZ)


def extra_places(curated=True):
    places = normalize_overpass(json.loads(fixture_text("overpass_city_extra.json")))
    if curated:
        overrides.apply(places, overrides.load())
    return places


def lite(places):
    return [PlaceLite(id=p.id, name=p.name, lat=p.lat, lon=p.lon, category=p.category, name_ru=p.name_ru,
                      name_kk=p.name_kk, name_en=p.name_en, aliases=p.aliases, address=p.address,
                      opening_hours=p.opening_hours, subcategory=p.subcategory)
            for p in places if p.subcategory != "playground"]


@pytest.fixture
def city(sample_places):
    return sample_places + extra_places()


@pytest.fixture
def loc(city):
    return Locator(lite(city))


def where(loc, text, **kw):
    a = loc.resolve(understand(text, **kw))
    return a.place_id if a else None


# ---------------- where the person is ----------------
def test_osm_copies_collapse_and_one_word_never_wins(loc):
    assert where(loc, "Я возле Mega Silk Way, хочу прогуляться") == "osm:way/2010"
    assert where(loc, "я у Mega Silk Way с ребёнком 5 лет, есть 2 часа") == "osm:way/2010"  # not the café "Coffee Way"
    assert where(loc, "в субботу вечером у Mega Silk Way") == "osm:way/2010"               # not the café "Вечер"
    assert where(loc, "я у Абу Даби Плаза") == where(loc, "у Абу-Даби Плазы") == "osm:way/9003"  # not "Astana plaza"
    assert where(loc, "у Пирамиды") == "osm:way/9007"            # a node and a building of one palace: one place
    assert where(loc, "у Астана Арены") == "osm:way/9011"        # the stadium, not the art object or the LRT stop


def test_case_forms_and_kazakh_postpositions(loc):
    assert where(loc, "я у Национального музея") == "osm:node/1001"
    assert where(loc, "я у Дворца мира") == "osm:way/9007"
    assert where(loc, "Бәйтерек жанындамын, бір сағат бар") == "osm:node/1003"
    assert where(loc, "Хан Шатырдың қасындамын") == "osm:way/2004"
    assert where(loc, "хочу кофе") is None and where(loc, "хочу в театр") is None   # words, not places


def test_place_after_another_preposition(loc):
    for text in ("хочу на концерт, я у Байтерека", "завтра в 11 буду у Байтерека", "в 2 ночи у Байтерека хочу поесть"):
        assert where(loc, text) == "osm:node/1003", text
    assert where(loc, "стендап в субботу, я у Хан Шатыра") == "osm:way/2004"


def test_named_place_beats_gps_only_when_the_person_is_there(loc):
    gps = {"lat": EXPO[0], "lon": EXPO[1]}   # the bot sends the last shared location with every message
    assert where(loc, "кофе у Хан Шатыра", **gps) == where(loc, "что есть в Хан Шатыре", **gps) == "osm:way/2004"
    assert loc.resolve(understand("хочу в Хан Шатыр", **gps)).source == "gps"   # where to go, not where I am
    assert loc.resolve(understand("кофе", **gps)).source == "gps"


def test_airport_is_nqz_and_curated_aliases_work_before_a_reimport(sample_places):
    loc = Locator(lite(extra_places(curated=False) + sample_places))   # the skydiving airfield comes first
    assert loc.airport().name == "Международный аэропорт Нурсултан Назарбаев"
    assert where(loc, "я у ЕНУ") == "osm:relation/9017"                 # alias from place_overrides.yaml


# ---------------- what was asked ----------------
@pytest.mark.parametrize("text,minutes", [
    ("есть два часа", 120), ("мы у Хан Шатыра, часа два", 120), ("полтора часа", 90), ("one hour", 60),
    ("two hours", 120), ("екі сағат бар", 120), ("в 2 часа ночи", None),
])
def test_budget_in_words(text, minutes):
    assert parse_budget(text)[1] == minutes


def test_clock_times_deadlines_and_event_kinds():
    it = parse_rules("в 2 ночи у Хан Шатыра хочу поесть")
    assert it.start_time.hour == 2 and "night" in it.needs and it.budget_max is None
    assert parse_rules("сегодня в 20").start_time.hour == 20
    it = parse_rules("завтра в 11 буду у Байтерека")
    assert (it.start_time.hour, it.start_days) == (11, 1)
    assert parse_rules("свободна до 8 вечера").deadline_time.hour == 20
    assert parse_rules("гуляю до 18").deadline_time.hour == 18 and parse_rules("дети до 7 лет").deadline is None
    assert parse_rules("хочу на концерт").event_kinds == ["concert"] and parse_rules("стендап").event_kinds == ["standup"]
    assert event_kind_fits({"kind": "other", "title": "Концерт «Хиты на все времена»"}, ["concert"])  # mis-filed
    assert not event_kind_fits({"kind": "kids", "title": "Жасыл Жұма"}, ["concert"])


# ---------------- the answer ----------------
def ev(eid, title, kind, place_id, lat, lon, start, end, state, now):
    mins = int((start - now).total_seconds() // 60)
    return {"event_id": eid, "title": title, "kind": kind, "venue": None, "place_id": place_id, "distance_m": 0,
            "starts_at": start, "ends_at": end, "state": state,
            "minutes_to_start": mins if state in ("soon", "later_today") else None,
            "minutes_left": int((end - now).total_seconds() // 60) if state == "now" else None, "ends_on": None,
            "days_left": None, "labels": [], "confidence": 0.9, "is_free": False, "price_text": None,
            "url": f"https://example.org/{eid}", "sources": [], "description": None, "lat": lat, "lon": lon}


class WithEvents(DbData):
    def __init__(self, conn, events):
        super().__init__(conn)
        self.extra = events

    def whats_on(self, lat, lon, radius_m, at, horizon_min):
        return super().whats_on(lat, lon, radius_m, at, horizon_min) + [
            e for e in self.extra if travel.haversine_m(lat, lon, e["lat"], e["lon"]) <= radius_m]


@pytest.fixture
def loaded(db, city):
    load_everything(db, city)
    return db


def test_free_until_a_time_is_the_budget(loaded):
    ans = Assistant(DbData(loaded)).ask("я у Хан Шатыра, свободен до 18:00", at=at(16, 18), weather_file=MILD)
    budget = {c.key: c for c in ans.chips}["budget"]
    assert ans.intent.budget_max == 102 and not budget.assumed and ans.context.window_end == at(18)
    assert "погода: из файла (демо)" in ans.used_data   # a demo file is not "Open-Meteo"


def test_the_place_you_are_at_and_a_closed_mall_at_night(loaded):
    a = Assistant(DbData(loaded))
    day = a.ask("я у Хан Шатыра, хочу кофе и тихо", at=at(16), weather_file=MILD)
    assert day.items and "Хан Шатыр" not in [i.title for i in day.items]
    night = a.ask("я у Хан Шатыра, хочу поесть", at=at(2, day=24), weather_file=MILD)
    assert "Starbucks" not in [i.title for i in night.items]
    r = next(r for r in night.rejected if r.title == "Starbucks")
    assert r.reason_code == "closed" and r.reason == "внутри «Хан Шатыр», а он закрыт, откроется в 10:00"


def test_a_ride_inside_a_mall_is_not_a_landmark(loaded):
    ans = Assistant(DbData(loaded)).ask("Я у Хан Шатыра, хочу прогуляться 30–60 минут", at=at(16, 30),
                                        weather_file=MILD)
    assert "Gold Mine Coaster" not in [i.title for i in ans.items]
    ride = K.Cand(kind="place", id="x", title="Gold Mine Coaster", lat=0, lon=0, category="landmark",
                  subcategory="attraction", attrs={"level": "4"})
    assert K.indoor_attraction(ride) and K.need_match(ride, "walk")[0] == K.need_match(ride, "photo")[0] == "unknown"
    assert "снаружи" not in K.idea(ride)[0]


def test_an_event_tonight_is_no_reason_at_1_30(loaded):
    now = at(1, 30, day=24)
    quiz = ev("q", "Квиз", "other", "osm:node/1007", 51.0905, 71.417, at(18, day=24), at(20, day=24), "later_today", now)
    ans = Assistant(WithEvents(loaded, [quiz])).ask("Ищу необычную ночную активность", lat=EXPO[0], lon=EXPO[1],
                                                    at=now, weather_file=MILD)
    assert ans.items and not any("18:00" in w for i in ans.items for w in i.why)


def test_event_kind_filters_and_kids_shows_are_for_kids(loaded):
    now = at(18)
    kids = ev("k", "Жасыл Жұма", "kids", "osm:way/9003", 51.1220, 71.4279, at(17), at(19, 30), "now", now)
    concert = ev("c", "Вечер романсов", "concert", "osm:way/2002", 51.1307, 71.4146, at(19), at(21), "soon", now)
    a = Assistant(WithEvents(loaded, [kids, concert]))
    ans = a.ask("я у Байтерека, хочу на концерт", at=now, weather_file=MILD)
    assert ans.items[0].title == "Астана Опера" and ans.items[0].attached_event == "Вечер романсов"
    assert not any(i.attached_event == "Жасыл Жұма" or "Жасыл Жұма" in " ".join(i.why) for i in ans.items)
    family = a.ask("я у Байтерека с ребёнком, что интересного", at=now, weather_file=MILD)
    assert any(i.attached_event == "Жасыл Жұма" for i in family.items)


def test_the_route_is_the_picked_places(loaded):
    ans = Assistant(DbData(loaded)).ask("Я сейчас возле Mega Silk Way, хочу прогуляться 30–60 минут", at=at(15),
                                        weather_file=MILD)
    stops = [s.title for s in ans.route.stops]
    kinds = {i.title: "food" if i.category == "food" else i.subcategory for i in ans.items}
    assert set(stops) <= set(kinds) and not set(stops) & {r.title for r in ans.rejected}
    assert len({kinds[s] for s in stops}) == len(stops)   # no two cafés (or café + restaurant) in a row


def test_two_at_night_said_in_the_afternoon_is_tonight(loaded):
    ans = Assistant(DbData(loaded)).ask("я у Байтерека, в 2 ночи, куда пойти?", at=at(15), weather_file=MILD)
    assert ans.at == at(2, day=24) and "night" in ans.intent.needs


# ---------------- weather that does not hang the answer ----------------
class Resp:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


def test_weather_failures_are_cached_per_cell(monkeypatch):
    calls: list[tuple[str, float]] = []
    ok = {"weather": json.loads((FIX / "weather_mild.json").read_text(encoding="utf-8")),
          "air": json.loads((FIX / "air_good.json").read_text(encoding="utf-8"))}
    down = True

    class Http:
        @staticmethod
        def get(url, **kw):
            calls.append((url, kw["timeout"]))
            if down:
                raise httpx.ConnectTimeout("Open-Meteo hangs")
            return Resp(ok["air" if "air-quality" in url else "weather"])

    clock = [1000.0]
    monkeypatch.setattr(A, "httpx", Http)
    monkeypatch.setattr(A, "_time", SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(W, "_CACHE", {})
    monkeypatch.setattr(A, "_CACHE", {})
    for _ in range(5):  # five themed walks for one place
        assert W.get(*BAITEREK, at(15)) is None and A.get(*BAITEREK, at(15)) is None
    assert len(calls) == 2 and all(t <= 3 for _, t in calls)   # one weather + one air request, short timeout
    down, clock[0] = False, clock[0] + 301                     # five minutes later the cell is tried again
    for _ in range(5):
        assert W.get(*BAITEREK, at(15)).source == "Open-Meteo" and A.get(*BAITEREK, at(15)) is not None
    assert len(calls) == 4
    assert W.get(0, 0, at(15), MILD).source == "из файла (демо)"


# ---------------- the data pipeline ----------------
def test_exhibition_period_with_a_vernissage_time_is_not_a_daily_window():
    today, museum = date(2026, 10, 20), "Tu-Su 10:00-18:00; Tu[-1] off"
    opening = ExtractedEvent(title="Отырар–Туркестан", kind="exhibition", date_start="2026-10-09",
                             date_end="2026-11-09", time_start="16:00")   # "открытие 9 октября в 16:00"
    s = build_sessions(opening, "exhibition", venue_hours=museum, today=today)
    assert s and all(x.hours_from == "venue" for x in s) and s[0].starts_at.hour == 10 and s[0].ends_at.hour == 18
    assert all(not x.time_known for x in build_sessions(opening, "exhibition", venue_hours=None, today=today))
    daily = ExtractedEvent(title="x", kind="exhibition", date_start="2026-10-20", date_end="2026-10-25",
                           time_start="11:00", time_end="21:00")
    assert build_sessions(daily, "exhibition", venue_hours=None, today=today)[0].ends_at.hour == 21
    series = ExtractedEvent(title="Киноужин", kind="film", date_start="2026-10-20", date_end="2026-10-22",
                            time_start="19:00")
    assert [x.starts_at.hour for x in build_sessions(series, "film", venue_hours=None, today=today)] == [19, 19, 19]


def test_address_match_needs_names_that_agree(city):
    m = VenueMatcher(lite(city))

    def pid(venue, address=None, kind=None):
        r = m.match(venue, address, kind)
        return r.place.id if r else None

    assert pid("Дворец Жастар", "пр. Республики, 34, Астана", "concert") == "osm:way/9015"   # curated alias
    raw = VenueMatcher(lite(extra_places(curated=False)))   # before the alias: unmatched, but never wrong
    assert raw.match("Дворец Жастар", "пр. Республики, 34, Астана", "concert") is None
    assert raw.match("Дворец Жастар", "пр. Республики, 34, Астана", "other") is None   # not the hotel "Жастар"
    assert pid(None, "улица Республики, 34") == "osm:node/9014"   # an address alone still works
    assert pid("Кафе", "улица Республики, 34") == "osm:node/9014"
    assert pid(None, "пр. Тауельсиздик, 54") == "osm:node/1001"


def test_nothing_on_foot_tries_a_taxi_unless_walking_was_asked(loaded):
    now = at(19, 10)
    standup = ev("s", "Проверочный стендап", "standup", None, 51.1600, 71.4700, at(20), at(21, 30), "soon", now)
    standup["description"] = "Сбор: 19:00Начало: 20:00Выступят: комики"
    a = Assistant(WithEvents(loaded, [standup]))
    ans = a.ask("я у Хан Шатыра, хочу на стендап", at=now, weather_file=MILD)
    assert [i.title for i in ans.items] == ["Проверочный стендап"] and ans.items[0].by == "taxi"
    assert any("на такси" in n for n in ans.notes) and {c.key: c for c in ans.chips}["mode"].label == "на такси"
    assert ans.items[0].what_to_do.startswith("Сбор: 19:00 · Начало: 20:00")   # glued listing lines come apart
    walk = a.ask("я у Хан Шатыра, хочу на стендап, пешком", at=now, weather_file=MILD)
    assert not walk.items and walk.intent.mode == "walk"


def test_a_named_kind_of_place_is_the_answer():
    """"Библиотека" was read as a stroll and got cafés. A kind named outright answers with that kind only."""
    from qydyr_data.assistant.intent import parse_rules, place_kinds, understand
    assert place_kinds("библиотека") == ["library"] and place_kinds("хочу на каток") == ["ice_rink"]
    assert place_kinds("кинотеатр") == ["cinema"] and place_kinds("театр") == ["theatre"]   # "кинотеатр" is not a theatre
    assert place_kinds("каталог") == [] and place_kinds("кофе и тихо") == []
    it = understand("Библиотека")
    assert it.place_kinds == ["library"] and it.needs == [] and not any("погулять" in a for a in it.assumptions)
    assert "food" in parse_rules("кафе у Хан Шатыра").needs


def test_what_the_person_does_not_want_is_left_out(loaded):
    """"не хочу кофе и кафе" still got a café: the words were picked as wishes, the "не" was not read."""
    from qydyr_data.assistant.intent import negated
    assert {"coffee", "food"} <= set(negated("не хочу кофе и кафе")[0])
    it = understand("я у Хан Шатыра, не хочу кофе и кафе, просто погулять")
    assert "coffee" in it.avoid and "coffee" not in it.needs and "food" not in it.needs
    assert understand("кроме ресторанов, что-нибудь интересное").avoid == ["food"]
    ans = Assistant(DbData(loaded)).ask("я у Хан Шатыра, не хочу кофе и кафе", at=at(16), weather_file=MILD)
    assert ans.items and not any(i.subcategory in {"cafe", "coffee_shop", "restaurant", "fast_food"} for i in ans.items)
    assert any(c.key == "avoid" for c in ans.chips)
    assert any(r.reason.startswith("вы просили без") for r in ans.rejected)


def test_a_follow_up_keeps_what_was_said_before(loaded):
    """Every next message was a new request: "а без кофе?" after "я у Хан Шатыра, хочу кофе" forgot the place."""
    from qydyr_data.assistant.intent import follow_up
    prev = understand("я у Хан Шатыра, с ребёнком, на час", defaults=False)
    it = follow_up(prev, understand("а вечером?", defaults=False))
    assert it.with_kids and it.budget_max == 60 and it.start_time is not None
    it = follow_up(understand("кофе", defaults=False), understand("а ещё поесть", defaults=False))
    assert {"coffee", "food"} <= set(it.needs)
    it = follow_up(understand("библиотека", defaults=False), understand("а кинотеатр?", defaults=False))
    assert it.place_kinds == ["cinema"]
    it = follow_up(understand("погулять и кофе", defaults=False), understand("без кофе", defaults=False))
    assert "coffee" not in it.needs and "walk" in it.needs and it.avoid == ["coffee"]

    a = Assistant(DbData(loaded))
    first = a.ask("я у Хан Шатыра, хочу кофе", at=at(16), weather_file=MILD)
    ctx = a.understood
    nxt = a.ask("а с ребёнком?", at=at(16), weather_file=MILD, context=ctx, context_anchor=first.anchor)
    assert nxt.anchor is not None and nxt.anchor.label == first.anchor.label
    assert nxt.intent.with_kids and "coffee" in nxt.intent.needs
    fresh = a.ask("забудь, я у Байтерека", at=at(16), weather_file=MILD, context=ctx, context_anchor=first.anchor)
    assert fresh.anchor.label != first.anchor.label and not fresh.intent.with_kids


def test_hours_and_minutes_together():
    """"1 час 30 минут" was read as an hour: the minutes after the hours count too."""
    assert parse_budget("у меня 1 час 30 минут") == (90, 90)
    assert parse_budget("2ч 15м") == (135, 135) and parse_budget("час и 20 минут") == (80, 80)
    assert parse_budget("1 hour 30 min") == (90, 90) and parse_budget("1 час") == (60, 60)
