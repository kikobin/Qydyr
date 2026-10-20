"""Guided walks: the curated content (every fact has a source), the walk engine (arrival, next stop,
off route), nudges on the way, the mascot's outfit, the API and the bot with live location."""
import asyncio
import re
from datetime import datetime

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import SendMessage, SendSticker
from aiogram.types import CallbackQuery, Location, Update
from fastapi.testclient import TestClient

from qydyr_data import settings
from qydyr_data.api import app, get_data
from qydyr_data.assistant import tours as TOURS
from qydyr_data.assistant.air import Air
from qydyr_data.assistant.evaluate import EMOJI
from qydyr_data.assistant.mascot import mascot_mood
from qydyr_data.assistant.recommend import DbData
from qydyr_data.assistant.weather import Weather
from qydyr_data.bot.app import QydyrBot
from qydyr_data.bot.backend import LocalBackend

from .conftest import TEST_DSN
from .test_bot import PRIVATE, USER, FakeSession, as_message, ids, msg
from .test_pipeline_db import load_everything

FIX = settings.ROOT / "tests" / "fixtures" / "tours_sample.yaml"
AT = datetime(2026, 10, 23, 19, 10, tzinfo=settings.TZ)


@pytest.fixture
def sample_tours(monkeypatch):
    monkeypatch.setattr(TOURS, "TOURS_FILE", FIX)
    return TOURS.load_tours(FIX)


def weather(feels=5.0, wind=3.0, code=1, precip=0.0, snow=0.0, aqi=None):
    sunrise, sunset = datetime(2026, 10, 23, 6, 49, tzinfo=settings.TZ), datetime(2026, 10, 23, 17, 7, tzinfo=settings.TZ)
    w = Weather(at=AT, temp_c=feels, feels_c=feels, wind_ms=wind, precip_mm=precip, snow_cm=snow, code=code,
                sunrise=sunrise, sunset=sunset)
    if aqi is not None:
        w.air = Air(at=AT, pm2_5=60, pm10=80, european_aqi=aqi)
    return w


# ------------------------------------------------------------------ the content
def test_curated_tours_are_sourced_and_clean():
    tours = TOURS.load_tours()
    if not tours:
        pytest.skip("config/tours.yaml is not there yet")
    for t in tours.values():
        assert len(t.stops) >= 3, t.id
        assert t.title.en and t.title.kk, t.id
        prev = None
        for s in t.stops:
            assert 1 <= len(s.facts) <= 4, (t.id, s.id)
            assert s.name.en, (t.id, s.id)
            for f in s.facts:
                assert re.match(r"^https?://", f.source), (t.id, s.id, f.source)
                assert len(f.ru) <= 300 and f.en, (t.id, s.id, f.ru)
                assert not EMOJI.search(f.ru + (f.en or "")), (t.id, s.id)
            if prev is not None:   # walkable: next stop within ~1.5 km
                assert TOURS.haversine_m(prev.lat, prev.lon, s.lat, s.lon) < 1500, (t.id, s.id)
            prev = s


# ------------------------------------------------------------------ the engine
def test_arrival_next_stop_and_done(sample_tours):
    t = sample_tours["test_expo"]
    far = TOURS.progress(t, 51.0950, 71.4300)
    assert far["arrived"] is None and far["next"] == "sphere" and far["next_m"] > 900 and far["off_route"] is False
    at_sphere = TOURS.progress(t, 51.08905, 71.41605)
    assert at_sphere["arrived"] == "sphere" and at_sphere["next"] == "art" and at_sphere["visited"] == ["sphere"]
    # GPS accuracy widens the circle a little, never endlessly
    edge = (51.0890 - 120 / 111320, 71.4160)   # 120 m south of the sphere, away from the other stops
    assert TOURS.progress(t, *edge)["arrived"] is None
    assert TOURS.progress(t, *edge, accuracy_m=30)["arrived"] == "sphere"
    # skipping a stop: the next one counts, the skipped one comes back at the end
    skip = TOURS.progress(t, 51.0880, 71.4190, ["sphere"])
    assert skip["arrived"] == "square" and skip["next"] == "art" and not skip["done"]
    done = TOURS.progress(t, 51.0896, 71.4145, ["sphere", "square"])
    assert done["arrived"] == "art" and done["done"] and done["next"] is None


def test_off_route_only_after_the_start(sample_tours):
    t = sample_tours["test_expo"]
    assert TOURS.progress(t, 51.1100, 71.4600)["off_route"] is False            # still on the way to the start
    assert TOURS.progress(t, 51.1100, 71.4600, ["sphere"])["off_route"] is True
    assert TOURS.progress(t, 51.0893, 71.4152, ["sphere"])["off_route"] is False  # between the stops


def test_views_languages_and_meta(sample_tours):
    t = sample_tours["test_expo"]
    ru, en, kk = (TOURS.tour_view(t, lang) for lang in ("ru", "en", "kk"))
    assert ru["meta"] == "3 точки · 40 мин · 0,8 км" and en["meta"] == "3 stops · 40 min · 0.8 km"
    assert kk["meta"].startswith("3 нүкте") and kk["title"] == "Сынақ серуені"
    f0 = en["stops_list"][0]["facts"]
    assert f0[0]["text"] == "A test fact about the sphere." and f0[0]["source_name"] == "Википедия"
    assert f0[1]["lang"] == "ru" and f0[1]["source_name"] == "Tengrinews"   # no translation, no key: Russian stays
    assert ru["intro"].startswith("Я Суырбек") and "4 факта" in ru["intro"]
    assert ru["hours"]["source"] == "https://example.org/hours" and len(ru["geometry"]) == 3
    assert [leg["walk_min"] for leg in ru["legs"]] and ru["stops_list"][2]["tip"].startswith("Сделайте")


def test_stop_story_from_facts_and_from_claude(sample_tours):
    t = sample_tours["test_expo"]
    s = t.stops[0]
    plain = TOURS.stop_story(t, s, "ru")
    assert plain["source"] == "facts" and "Тестовый факт" in plain["text"] and "Посмотрите вверх" in plain["text"]

    class FakeLLM:
        def text(self, *, system, user, max_tokens):
            assert "ТОЛЬКО факты" in system and "Тестовый факт" in user
            return "Мы у сферы. Тестовый факт про сферу."
    TOURS._STORIES.clear()
    told = TOURS.stop_story(t, s, "en", llm=FakeLLM())
    assert told["source"] == "claude" and told["text"].startswith("Мы у сферы")


def test_nudges_events_warmth_and_air():
    events = [
        {"event_id": "e1", "title": "Ночь в музее", "venue": "Национальный музей", "state": "now", "distance_m": 120},
        {"event_id": "e2", "title": "Джаз-вечер", "venue": "бар «Луна»", "state": "soon", "minutes_to_start": 25, "distance_m": 300},
        {"event_id": "e3", "title": "Далёкий концерт", "venue": "где-то", "state": "soon", "minutes_to_start": 20, "distance_m": 900},
        {"event_id": "e4", "title": "Потом", "venue": "рядом", "state": "soon", "minutes_to_start": 120, "distance_m": 100},
    ]
    out = TOURS.nudges(events, lang="ru")
    assert [n["event_id"] for n in out] == ["e1", "e2"]
    assert out[1]["text"] == "Через 25 мин в 300 м начнётся «Джаз-вечер» — бар «Луна»."
    frost = weather(feels=-22, wind=6)
    warm = [{"place_id": "p1", "name": "Кофейня «Тихий угол»", "name_ru": "Кофейня «Тихий угол»", "distance_m": 150}]
    w_out = TOURS.nudges([], weather=frost, next_leg_min=18, warm=warm, lang="ru")
    assert w_out[0]["kind"] == "warm" and "−22°" not in w_out[0]["text"] and "-22°" in w_out[0]["text"]
    assert "комфортно не больше 10 мин" in w_out[0]["text"] and "Тихий угол" in w_out[0]["text"]
    assert TOURS.nudges([], weather=weather(feels=-22), next_leg_min=5, warm=warm) == []
    smog = TOURS.nudges([], weather=weather(feels=-5, aqi=90), lang="en")
    assert smog[0]["kind"] == "air" and smog[0]["text"].startswith("The air is bad")


def test_generated_walk_becomes_a_tour():
    walk = {"id": "warm", "title": "Тёплый маршрут", "subtitle": "почти всё время под крышей", "meta": "2 точки · 60 мин",
            "answer": {"items": [{"title": "Нур Алем", "place_id": "p1", "lat": 51.089, "lon": 71.416, "status": "открыто до 20:00",
                                  "why": ["интерактив: да"], "what_to_do": "поднимитесь на верхний этаж", "sources": ["OpenStreetMap"],
                                  "indoor": "indoor"},
                                 {"title": "Кофейня", "place_id": "p2", "lat": 51.0912, "lon": 71.4123, "status": "открыто до 22:00",
                                  "why": [], "what_to_do": "", "sources": [], "indoor": "indoor"}],
                       "route": {"stops": [{"title": "Нур Алем", "lat": 51.089, "lon": 71.416, "place_id": "p1"},
                                           {"title": "Кофейня", "lat": 51.0912, "lon": 71.4123, "place_id": "p2"}], "total_min": 60}}}
    t = TOURS.tour_from_answer(walk)
    assert t["kind"] == "walk" and t["stops"] == 2 and t["stops_list"][0]["facts"][0]["text"] == "Открыто до 20:00"
    assert t["stops_list"][0]["tip"] == "поднимитесь на верхний этаж" and t["stops_list"][1]["tip"] is None
    assert t["stops_list"][0]["outdoor"] is False and t["geometry"][0] == [71.416, 51.089]


def test_mascot_dresses_for_the_weather():
    assert mascot_mood(None) == "hello"
    assert mascot_mood(weather(feels=-15), AT.replace(hour=12)) == "cold"
    assert mascot_mood(weather(feels=-5, aqi=95), AT.replace(hour=12)) == "smog"
    assert mascot_mood(weather(feels=8, code=61, precip=1.2), AT.replace(hour=12)) == "rain"
    assert mascot_mood(weather(feels=2, code=73, snow=0.5), AT.replace(hour=12)) == "hello"   # snow at +2 is not rain
    assert mascot_mood(weather(feels=8), AT) == "night"                                        # 19:10, sunset 17:07
    assert mascot_mood(weather(feels=26), AT.replace(hour=13)) == "summer"
    assert mascot_mood(weather(feels=12), AT.replace(hour=13)) == "hello"


# ------------------------------------------------------------------ the API
def client(db, sample_places, monkeypatch):
    load_everything(db, sample_places)
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", None)
    monkeypatch.setattr(settings, "DEMO_NOW", "2026-10-23T19:10")
    monkeypatch.setattr(settings, "WEATHER_FILE", str(settings.ROOT / "tests/fixtures/weather_mild.json"))
    monkeypatch.setattr(settings, "AIR_FILE", str(settings.ROOT / "tests/fixtures/air_good.json"))
    app.dependency_overrides[get_data] = lambda: DbData(db)
    return TestClient(app)


def test_tours_api(db, sample_places, monkeypatch, sample_tours):
    c = client(db, sample_places, monkeypatch)
    try:
        cards = c.get("/tours", params={"lang": "en"}).json()
        assert cards == [TOURS.tour_card(sample_tours["test_expo"], "en")] and cards[0]["title"] == "Test walk"
        full = c.get("/tours/test_expo", params={"lang": "ru"}).json()
        assert full["stops_list"][1]["name"] == "Арт-объект «Ветер степи»" and full["geometry_source"] == "straight"
        assert c.get("/tours/nope").status_code == 404
        pr = c.post("/tours/test_expo/progress", json={"lat": 51.08905, "lon": 71.41605, "visited": []}).json()
        assert pr["arrived"] == "sphere" and pr["next"] == "art"
        story = c.get("/tours/test_expo/stops/art/story").json()
        assert story["source"] == "facts" and "Факт про арт-объект" in story["text"]
        assert c.get("/tours/test_expo/stops/nope/story").status_code == 404
        w = c.get("/weather", params={"lat": 51.09, "lon": 71.41}).json()
        assert w["mood"] == "night" and w["dark"] is True and "свежо" in w["summary"]
        assert c.get("/weather", params={"lat": 51.09, "lon": 71.41, "weather_preset": "blizzard"}).json()["mood"] == "cold"
        # walking past the museum on Friday evening: the evening exhibition is on right here
        n = c.get("/walk/nudges", params={"lat": 51.1186, "lon": 71.4701, "lang": "ru"}).json()
        assert any(x["kind"] == "event" and "Ночь в музее" in x["text"] for x in n["nudges"]) and n["weather"]["mood"] == "night"
        # frost and a long leg ahead: a warm, open place on the way
        cold = c.get("/walk/nudges", params={"lat": 51.0900, "lon": 71.4165, "weather_preset": "frost",
                                             "next_lat": 51.1283, "next_lon": 71.4305}).json()
        assert any(x["kind"] == "warm" for x in cold["nudges"])
    finally:
        app.dependency_overrides.clear()


def test_walks_carry_a_tour_view(db, sample_places, monkeypatch, sample_tours):
    c = client(db, sample_places, monkeypatch)
    try:
        walks = c.get("/walks", params={"lat": 51.0900, "lon": 71.4165, "at": "2026-10-23T15:00:00"}).json()
        assert walks and all(w["tour"]["kind"] == "walk" and w["tour"]["stops_list"] for w in walks)
    finally:
        app.dependency_overrides.clear()


# ------------------------------------------------------------------ the bot
@pytest.fixture
def bot_harness(db, sample_places, monkeypatch, sample_tours):
    load_everything(db, sample_places)
    monkeypatch.setattr(settings, "DATABASE_URL", TEST_DSN)
    monkeypatch.setattr(settings, "DEMO_NOW", "2026-10-23T19:10")
    monkeypatch.setattr(settings, "WEATHER_FILE", str(settings.ROOT / "tests/fixtures/weather_mild.json"))
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", None)
    session = FakeSession()
    bot = Bot("42:TEST", session=session)
    qb = QydyrBot(LocalBackend(), bot_username="qydyr_bot", webapp_url="https://qydyr.example/app/")
    qb.walk_interval_s = 0
    dp = Dispatcher()
    dp.include_router(qb.router)
    return bot, dp, qb, session


def feed(bot, dp, **upd):
    return asyncio.run(dp.feed_update(bot, Update(update_id=next(ids), **upd)))


def cb(data, message):
    return CallbackQuery(id=str(next(ids)), from_user=USER, chat_instance="w", message=message, data=data)


def test_bot_guided_walk_manual(bot_harness):
    bot, dp, qb, s = bot_harness
    feed(bot, dp, message=msg("/walks"))
    tours_msg = next(m for m in s.sent(SendMessage) if "Прогулки с гидом" in m.text)
    btn = tours_msg.reply_markup.inline_keyboard[0][0]
    assert btn.callback_data == "tw:test_expo" and btn.text.startswith("Тестовая прогулка · 3 точки")
    feed(bot, dp, callback_query=cb("tw:test_expo", as_message(tours_msg, PRIVATE)))
    card = s.sent(SendMessage)[-1]
    assert "<b>Тестовая прогулка</b>" in card.text and "1. Нур Алем" in card.text
    rows = card.reply_markup.inline_keyboard
    assert rows[0][0].callback_data == "tl:test_expo" and rows[0][1].callback_data == "tm:test_expo"
    assert rows[1][0].web_app.url == "https://qydyr.example/app/?tour=test_expo"
    feed(bot, dp, callback_query=cb("tm:test_expo", as_message(card, PRIVATE)))
    assert s.sent(SendSticker), "the mascot says hi at the start"
    start = s.sent(SendMessage)[-1]
    assert "Первая точка: Нур Алем" in start.text and start.reply_markup.inline_keyboard[0][0].callback_data == "ta:test_expo:sphere"
    feed(bot, dp, callback_query=cb("ta:test_expo:sphere", as_message(start, PRIVATE)))
    stop = s.sent(SendMessage)[-1]
    assert stop.text.startswith("<b>1/3 · Нур Алем</b>") and '<a href="https://ru.wikipedia.org/wiki/Test">Википедия</a>' in stop.text
    assert "Посмотрите вверх" in stop.text and "Дальше: Арт-объект «Ветер степи»" in stop.text
    assert stop.reply_markup.inline_keyboard[0][0].callback_data == "ta:test_expo:art"
    feed(bot, dp, callback_query=cb("te:test_expo", as_message(stop, PRIVATE)))
    done = s.sent(SendMessage)[-1]
    assert "завершена" in done.text and "Точек: 1 из 3" in done.text and qb.state.session(7).walk is None


def test_bot_guided_walk_live_location(bot_harness):
    bot, dp, qb, s = bot_harness
    feed(bot, dp, message=msg("/start t_test_expo"))
    card = s.sent(SendMessage)[-1]
    assert "<b>Тестовая прогулка</b>" in card.text
    feed(bot, dp, callback_query=cb("tl:test_expo", as_message(card, PRIVATE)))
    assert "Транслировать геопозицию" in s.sent(SendMessage)[-1].text
    live = msg(location=Location(latitude=51.0950, longitude=71.4300, live_period=900))
    feed(bot, dp, message=live)                     # far away: nothing yet
    n_before = len(s.sent(SendMessage))
    # live location ticks arrive as edits of the same message
    tick = live.model_copy(update={"location": Location(latitude=51.08905, longitude=71.41605, live_period=900),
                                   "edit_date": 1})
    feed(bot, dp, edited_message=tick)
    arrived = s.sent(SendMessage)[n_before:]
    assert arrived and arrived[0].text.startswith("<b>1/3 · Нур Алем</b>")
    assert qb.state.session(7).walk.visited == ["sphere"]
    for lat, lon in [(51.0896, 71.4145), (51.0880, 71.4190)]:
        feed(bot, dp, edited_message=tick.model_copy(update={"location": Location(latitude=lat, longitude=lon, live_period=900)}))
    texts = [m.text for m in s.sent(SendMessage)]
    assert any(x.startswith("<b>3/3 · Сквер у EXPO</b>") for x in texts)
    assert "завершена" in texts[-1] and "Точек: 3 из 3, фактов: 4" in texts[-1]
    assert qb.state.session(7).walk is None
    assert len(s.sent(SendSticker)) >= 3     # hello, start, finish


def test_location_without_walk_still_answers(bot_harness):
    bot, dp, qb, s = bot_harness
    feed(bot, dp, message=msg(location=Location(latitude=51.0900, longitude=71.4165)))
    assert "<b>1." in s.sent(SendMessage)[-1].text


def test_stickers_exist_for_every_mood():
    from qydyr_data.assistant.mascot import MOODS
    from PIL import Image
    folder = settings.WEB_DIR / "assets" / "mascot"
    for mood in MOODS:
        img = Image.open(folder / f"{mood}.webp")
        assert img.size == (512, 512) and img.mode in ("RGBA", "LA", "P"), mood


def test_map_pins_stay_absolutely_positioned():
    # MapLibre places a marker with a transform on an absolutely positioned element. A "relative" pin falls back
    # into the normal flow, and every pin then drifts sideways by the width of the pins before it.
    css = (settings.WEB_DIR / "assets" / "app.css").read_text(encoding="utf-8")
    rule = re.search(r"^\.pin \{([^}]*)\}", css, re.M).group(1)
    assert "position: absolute" in rule and "relative" not in rule.replace('"relative"', "")


def test_map_moves_never_leave_padding_behind():
    # easeTo/flyTo keep `padding` for every later move and fitBounds adds its own on top: after one such move
    # the tour fit fails with "Map cannot fit within canvas". Single points are framed with an offset instead.
    js = (settings.WEB_DIR / "assets" / "map.js").read_text(encoding="utf-8")
    for call in re.findall(r"\.(?:easeTo|flyTo|jumpTo)\(\{[^}]*\}", js):
        assert "padding" not in call, call
