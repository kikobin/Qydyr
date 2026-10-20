"""New core features: why-not, decision funnel, chips with options, overrides, the time & weather
machine, air quality, sunset, leave-by, surprise, route geometry, place Q&A, themed walks."""
from datetime import date, datetime, timedelta

import pytest

from qydyr_data import settings
from qydyr_data.assistant import air as A
from qydyr_data.assistant import placeqa
from qydyr_data.assistant import weather as W
from qydyr_data.assistant.models import Anchor, Overrides
from qydyr_data.assistant.recommend import Assistant, DbData, fmt_budget
from qydyr_data.assistant.render import render, render_why_not
from qydyr_data.assistant.sun import sun_times
from qydyr_data.assistant.walks import build_walks

from .test_pipeline_db import load_everything

TZ = settings.TZ
FIX = settings.ROOT / "tests" / "fixtures"
MILD = FIX / "weather_mild.json"
EXPO = (51.0900, 71.4165)
EMOJI = [chr(c) for c in range(0x1F300, 0x1FAFF)] + ["ℹ", "⚠", "️"]


def at(h, m=0, day=23):
    return datetime(2026, 10, day, h, m, tzinfo=TZ)


# ---------------- units ----------------
def test_air_bands_penalty_warning():
    good = A.get(51.1, 71.4, at(15), FIX / "air_good.json")
    smog = A.get(51.1, 71.4, at(15), FIX / "air_smog.json")
    assert good.band in ("хороший", "приемлемый") and good.penalty == 0 and good.warning() is None
    assert smog.european_aqi >= 80 and smog.band == "очень плохой" and smog.penalty == 0.35
    assert "лучше в помещении" in smog.warning() and "PM2.5" in smog.warning()
    later = A.get(51.1, 71.4, at(20), FIX / "air_smog.json")  # hourly slot, not "current"
    assert later.european_aqi != smog.european_aqi


def test_weather_presets_and_comfort():
    real = W.get(51.1, 71.4, at(15), MILD)
    level, label, factors = real.comfort(at(15))
    assert level == 4 and label == "отлично для прогулки"
    bliz = W.preset("blizzard", at(15), real=real)
    assert bliz.simulated == "метель −25" and bliz.outdoor_penalty >= 0.85 and bliz.max_outdoor_min <= 10
    assert bliz.comfort(at(15))[0] == 0 and "симуляция" in bliz.summary()
    summer = W.preset("summer_evening", at(20))
    assert not summer.is_dark(at(20)) and summer.is_dark(at(21))  # summer sunset ~20:33
    smog = W.preset("smog", at(15))
    lvl, lbl, _ = smog.comfort(at(15))
    assert lvl == 0 and "воздух" in lbl
    with pytest.raises(KeyError):
        W.preset("tornado", at(15))


def test_sun_times_astana():
    rise, sset = sun_times(date(2026, 10, 23), 51.1282, 71.4304, TZ)
    assert (rise.hour, sset.hour) == (6, 17)          # UTC+5: ~06:49 and ~17:07
    real = W.get(51.1, 71.4, at(16, 30), MILD)
    assert real.golden_hour(at(16, 40)) and not real.golden_hour(at(15))


def test_fmt_budget():
    assert fmt_budget(45, 75) == "45–75 мин"
    assert fmt_budget(60, 120) == "1–2 ч"
    assert fmt_budget(90, 90) == "1,5 ч"
    assert fmt_budget(30, 30) == "30 мин"
    assert fmt_budget(100, 120) == "до 2 ч" and fmt_budget(20, 30) == "до 30 мин" and fmt_budget(30, 60) == "30–60 мин"


def test_overrides_merge_accumulates_needs():
    ov = Overrides().merged({"add_needs": ["coffee"]}).merged({"remove_needs": ["walk"]}).merged({"budget_max": 30})
    assert ov.add_needs == ["coffee"] and ov.remove_needs == ["walk"] and ov.budget_max == 30
    ov = ov.merged({"add_needs": ["walk"]})
    assert "walk" in ov.add_needs and "walk" not in ov.remove_needs


# ---------------- end to end on PostGIS ----------------
@pytest.fixture
def assistant(db, sample_places):
    load_everything(db, sample_places)
    return Assistant(DbData(db))


def test_why_not_and_funnel(assistant):
    ans = assistant.ask("Ищу необычную ночную активность (последние пару часов).", lat=EXPO[0], lon=EXPO[1],
                        at=at(23, 30), weather_file=MILD)
    steps = [f.n for f in ans.funnel]
    assert [f.key for f in ans.funnel] == ["nearby", "reachable", "open", "fit", "picked"]
    assert steps == sorted(steps, reverse=True) and steps[-1] == len(ans.items)
    closed = [r for r in ans.rejected if r.reason_code == "closed"]
    assert closed and all(r.reason.startswith("закрыто, откроется завтра в") for r in closed)
    assert all(r.title not in {i.title for i in ans.items} for r in ans.rejected)
    text = render_why_not(ans)
    assert text.startswith("Почему не другое") and "Нур Алем" in text


def test_chips_are_structured_and_assumed(assistant):
    ans = assistant.ask("что интересного рядом", lat=EXPO[0], lon=EXPO[1], at=at(15), weather_file=MILD)
    chips = {c.key: c for c in ans.chips}
    assert chips["budget"].assumed and chips["budget"].label == "45–75 мин"
    assert any(o.patch == {"budget_min": 100, "budget_max": 120} for o in chips["budget"].options)
    assert chips["mode"].assumed and not chips["kids"].active and not chips["only_events"].active
    assert "Понял так: ваша геолокация · 45–75 мин? · пешком?" in render(ans)


def test_overrides_change_the_answer(assistant):
    base = assistant.ask("что интересного рядом", lat=EXPO[0], lon=EXPO[1], at=at(19, 10), weather_file=MILD)
    only = assistant.ask("что интересного рядом", lat=EXPO[0], lon=EXPO[1], at=at(19, 10), weather_file=MILD,
                         overrides=Overrides(only_events=True))
    assert len(only.items) >= 1 and all(i.kind == "event" or i.attached_event for i in only.items)
    assert any(r.reason_code == "not_event" for r in only.rejected)
    assert len(base.items) > len(only.items)
    kids = assistant.ask("что интересного рядом", lat=EXPO[0], lon=EXPO[1], at=at(15), weather_file=MILD,
                         overrides=Overrides(with_kids=True, budget_min=100, budget_max=120))
    assert kids.intent.with_kids and "kids" in kids.intent.needs
    assert {c.key: c for c in kids.chips}["budget"].label == "до 2 ч"
    assert not {c.key: c for c in kids.chips}["budget"].assumed
    assert all(i.subcategory not in ("bar", "pub", "nightclub") for i in kids.items)


def test_blizzard_preset_keeps_people_inside(assistant):
    ans = assistant.ask("Я возле Mega Silk Way, хочу прогуляться час", at=at(15), weather_file=MILD,
                        weather_preset="blizzard")
    assert ans.notes[0].startswith("Симуляция погоды: «метель −25»")
    assert ans.items and all(i.indoor != "outdoor" for i in ans.items)
    assert ans.context.simulated == "метель −25" and ans.context.comfort.level == 0
    assert any(r.reason_code == "cold_walk" for r in ans.rejected)


def test_smog_blocks_outdoor_walks(assistant):
    ans = assistant.ask("Я возле Mega Silk Way, хочу прогуляться час", at=at(15), weather_file=MILD,
                        air_file=FIX / "air_smog.json")
    assert ans.items and all(i.indoor != "outdoor" for i in ans.items)
    assert any("Воздух очень плохой" in n for n in ans.notes)
    assert ans.context.air.band == "очень плохой" and ans.context.comfort.level == 0
    assert any(r.reason_code == "weather" and "воздух" in r.reason for r in ans.rejected)
    assert any("воздух: из файла (демо)" in u for u in ans.used_data)  # a demo file is not "Open-Meteo"


def test_leave_by_for_an_event_starting_soon(assistant):
    ans = assistant.ask("что интересного рядом", lat=EXPO[0], lon=EXPO[1], at=at(19, 10), weather_file=MILD)
    bar = next(i for i in ans.items if "Луна" in i.title)
    assert bar.starts_at == at(20) and bar.leave_by == at(20) - timedelta(minutes=bar.walk_min)
    assert "soon" in bar.labels and "evening" in bar.labels
    assert f"Выходите до {bar.leave_by.astimezone(TZ):%H:%M}" in render(ans)


def test_surprise_is_one_unexpected_pick(assistant):
    a = assistant.ask("", lat=EXPO[0], lon=EXPO[1], at=at(15), weather_file=MILD, surprise=True, seed=7)
    b = assistant.ask("", lat=EXPO[0], lon=EXPO[1], at=at(15), weather_file=MILD, surprise=True, seed=7)
    assert a.surprise and len(a.items) == 1 and a.items[0].title == b.items[0].title
    assert a.intent.needs[0] == "unusual" and a.route is None


def test_route_has_geometry_from_start(assistant):
    ans = assistant.ask("Я сейчас возле Mega Silk Way, хочу прогуляться 30–60 минут", at=at(15), weather_file=MILD)
    r = ans.route
    assert r and len(r.geometry) == len(r.stops) + 1 and r.geometry_source == "straight"
    assert r.geometry[0] == [ans.anchor.lon, ans.anchor.lat]
    assert all(s.lat and s.lon for s in r.stops)


def test_no_emoji_anywhere_in_text(assistant):
    for text in ("Что интересного рядом прямо сейчас?", "Хочу кофе с ребёнком"):
        ans = assistant.ask(text, lat=51.1186, lon=71.4701, at=at(19, 10), weather_file=MILD)
        out = render(ans) + render_why_not(ans)
        assert not any(e in out for e in EMOJI), out


def test_place_qa_answers_from_facts(db, sample_places):
    load_everything(db, sample_places)
    from qydyr_data import store
    cafe = next(p for p in sample_places if p.name == "Кафе «Коктем»")
    row = store.place(db, cafe.id, at(19, 10))
    qa = placeqa.answer(row, [], "до скольки работает?", at(19, 10))
    assert qa.certainty == "yes" and "открыто до 23:00" in qa.answer
    qa = placeqa.answer(row, [], "есть ли тут розетки?", at(19, 10))
    assert qa.certainty in ("likely", "unknown") and qa.answer.startswith("Розетка")
    qa = placeqa.answer(row, [], "можно ли с собакой?", at(19, 10))
    assert qa.certainty == "unknown" and "нет" in qa.answer

    class FakeLLM:
        def structured(self, **kw):
            assert "FACTS" in kw["user"]
            return placeqa.LLMQA(answer="В данных этого нет.", certainty="unknown", evidence="")
    qa = placeqa.answer(row, [], "можно ли с собакой?", at(19, 10), llm=FakeLLM())
    assert qa.source == "claude" and qa.certainty == "unknown"
    museum = next(p for p in sample_places if p.name.startswith("Национальный музей"))
    mrow = store.place(db, museum.id, at(19, 10))
    evs = [e for e in store.whats_on(db, mrow["lat"], mrow["lon"], 150, at(19, 10), 600) if e["place_id"] == museum.id]
    qa = placeqa.answer(mrow, evs, "что там сейчас идёт? вход бесплатный?", at(19, 10))
    assert "Ночь в музее" in qa.answer and "бесплатный" in qa.answer and qa.source == "data"


def test_themed_walks(assistant):
    anchor = Anchor(lat=EXPO[0], lon=EXPO[1], label="ваша геолокация", source="gps")
    walks = build_walks(assistant, anchor, at(15), weather_file=MILD)
    ids = [w["id"] for w in walks]
    assert "highlights" in ids and "warm" in ids
    for w in walks:
        assert w["answer"]["items"] and "мин" in w["meta"]
    assert (anchor.lat, anchor.lon) == EXPO  # the caller's anchor is not mutated
    kk = {w["id"]: w for w in build_walks(assistant, anchor, at(15), lang="kk", weather_file=MILD)}
    assert kk["warm"]["title"] == "Жылы маршрут" and "нүкте" in kk["warm"]["meta"]
    assert set(kk) == set(ids)                # the same walks, only the words change


def test_walk_meta_plural_forms():
    from qydyr_data.assistant.walks import walk_meta
    assert walk_meta(1, 40, None) == "1 точка · 40 мин"
    assert walk_meta(3, 90, 20) == "3 точки · 90 мин · на улице ~20 мин"
    assert walk_meta(5, 120, None) == "5 точек · 120 мин"
    assert walk_meta(1, 40, 5, "en") == "1 stop · 40 min · ~5 min outside"
    assert walk_meta(2, 60, None, "kk") == "2 нүкте · 60 мин"
