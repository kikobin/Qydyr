"""Assistant building blocks without a database."""
from datetime import datetime, timedelta

import pytest

from qydyr_data import settings
from qydyr_data.assistant import knowledge as K
from qydyr_data.assistant import travel
from qydyr_data.assistant import weather as W
from qydyr_data.assistant.group import combine, group_score, parse_members
from qydyr_data.assistant.intent import parse_budget, understand
from qydyr_data.assistant.locate import Locator
from qydyr_data.assistant.models import Anchor
from qydyr_data.assistant.privacy import cell_center, coarse, geohash
from qydyr_data.assistant.recommend import Scored
from qydyr_data.assistant.route import build_route

from .conftest import FIX

TZ = settings.TZ
AT = datetime(2026, 10, 23, 15, 0, tzinfo=TZ)


# ---------- understanding ----------
@pytest.mark.parametrize("text,lo,hi", [
    ("прогуляться 30–60 минут", 30, 60), ("на 1–2 часа", 60, 120), ("есть полчаса", 30, 30),
    ("у меня 2 часа", 120, 120), ("минут 40", 40, 40), ("пару часов", 120, 120), ("хочу кофе", None, None),
])
def test_budget(text, lo, hi):
    assert parse_budget(text) == (lo, hi)


def test_brief_requests_understood():
    it = understand("Еду с ребёнком — где рядом есть детские площадки или музей с интерактивами?")
    assert it.with_kids and it.needs[:2] == ["kids", "playground"] and "interactive" in it.needs
    it = understand("Я - турист, у меня 2 часа перед вылетом: что можно посмотреть поблизости?")
    assert it.deadline == "flight" and it.persona == "tourist" and it.budget_max == 120
    it = understand("Хочу тихое место с кофе и розеткой")
    assert it.needs == ["quiet", "coffee", "socket"]          # in the order the person said them
    assert "около часа (время не указано)" in it.assumptions  # shown back as an editable chip
    it = understand("я у Чупа-чупса, нас 4 человека, до 18:00")
    assert it.group_size == 4 and it.deadline == "time" and it.deadline_time.hour == 18


def test_kazakh_and_english():
    kk = understand("Жақын жерде кофе ішетін тыныш орын бар ма?")
    assert kk.lang == "kk" and set(kk.needs) == {"coffee", "quiet"}   # "бар ма" is not a bar
    en = understand("Any quiet coffee place with wifi near me?")
    assert en.lang == "en" and {"coffee", "quiet", "wifi"} <= set(en.needs)


def test_locator_understands_people(sample_places_lite):
    loc = Locator(sample_places_lite)

    def where(t):
        a = loc.resolve(understand(t))
        return a.label if a else None

    assert where("Я сейчас возле Mega Silk Way") == "Mega Silk Way"
    assert where("я у Чупа-чупса") == where("стою возле Байтерека") == "Бәйтерек"
    assert where("я у Хан Шатыра") == "Хан Шатыр"                      # Russian case ending
    assert where("Я у здании Экспо 2017") == "Нур Алем"
    assert where("сижу в AITU, перерыв 40 минут") == "Astana IT University"
    assert where("хочу кофе рядом") is None
    gps = loc.resolve(understand("кофе", lat=51.1, lon=71.4))
    assert gps.source == "gps"


# ---------- weather ----------
def test_weather_levels_and_warnings():
    cold = W.get(0, 0, AT, FIX / "weather_cold.json")
    assert cold.level == "Астана classic" and cold.outdoor_penalty >= 0.85 and cold.max_outdoor_min == 10
    assert "через 60 мин ожидается снег" in cold.warnings(AT)
    mild = W.get(0, 0, AT, FIX / "weather_mild.json")
    assert mild.level == "свежо" and mild.outdoor_penalty < 0.1
    assert mild.is_dark(AT.replace(hour=19)) and not mild.is_dark(AT)
    assert any("закат" in w for w in mild.warnings(AT))


# ---------- travel ----------
def test_walking_depends_on_who_and_weather():
    a, b = (51.0897, 71.4078), (51.0890, 71.4160)  # Mega -> Nur Alem, ~575 m straight
    adult = travel.walk_min(*a, *b)
    kid = travel.walk_min(*a, *b, kids=True)
    cold = W.get(0, 0, AT, FIX / "weather_cold.json")
    assert 8 <= adult <= 11 and kid > adult and travel.walk_min(*a, *b, weather=cold) > adult
    assert travel.walk_min(*a, *a) == 0
    assert 25 <= travel.taxi_min(51.1283, 71.4305, 51.0222, 71.4669) <= 40  # Baiterek -> airport


# ---------- knowledge ----------
def test_need_matching_is_honest():
    cafe_wifi = K.Cand(kind="place", id="1", title="c", lat=0, lon=0, category="food", subcategory="cafe",
                       indoor="indoor", attrs={"wifi": True})
    assert K.need_match(cafe_wifi, "coffee")[0] == "yes"
    cert, ev = K.need_match(cafe_wifi, "socket")
    assert cert == "likely" and "не гарантированы" in ev       # never claims sockets as a fact
    bar = K.Cand(kind="place", id="2", title="b", lat=0, lon=0, category="nightlife", subcategory="bar")
    assert K.need_match(bar, "kids")[0] == "no"
    museum = K.Cand(kind="place", id="3", title="m", lat=0, lon=0, category="culture", subcategory="museum",
                    indoor="indoor", attrs={"interactive": "yes"})
    assert K.need_match(museum, "kids")[0] == "yes" and K.need_match(museum, "walk", cold=True)[0] == "likely"
    assert K.need_match(museum, "walk")[0] == "unknown"


def test_idea_prefers_real_description():
    c = K.Cand(kind="place", id="1", title="m", lat=0, lon=0, subcategory="museum",
               attached=[{"description": "Залы золота открыты вечером. Вход свободный."}])
    assert K.idea(c) == ("Залы золота открыты вечером.", "data")
    assert K.idea(K.Cand(kind="place", id="2", title="p", lat=0, lon=0, subcategory="park"))[1] == "idea"


# ---------- group ----------
def test_least_misery():
    members = parse_members(["Айгерим: кофе и тихо", "Данияр: хочу на смотровую"])
    base = combine(understand("мы у Mega", defaults=False), members)
    assert base.group_size == 2 and {"coffee", "quiet", "view"} <= set(base.needs)
    cafe = K.Cand(kind="place", id="1", title="c", lat=0, lon=0, category="food", subcategory="cafe", indoor="indoor")
    view = K.Cand(kind="place", id="2", title="v", lat=0, lon=0, category="landmark", subcategory="viewpoint")
    s_cafe, expl = group_score(cafe, members, AT)
    s_view, _ = group_score(view, members, AT)
    assert expl[0].startswith("Айгерим: кофе — да")
    assert s_cafe < 0.5 and s_view < 0.5  # each leaves someone unhappy — nobody "wins" by average alone


# ---------- route ----------
def _scored(id, lat, lon, sub, visit, score, indoor="indoor", window_end=None):
    c = K.Cand(kind="place", id=id, title=id, lat=lat, lon=lon, subcategory=sub, indoor=indoor)
    return Scored(cand=c, walk=0, arrive=AT, start_at=None, window_end=window_end, visit=visit, score=score,
                  need_score=0.8, hits={})


def test_route_fits_budget_and_hours():
    anchor = Anchor(lat=51.0897, lon=71.4078, label="Mega", source="place")
    a = _scored("Арт", 51.0896, 71.4145, "artwork", (5, 10), 0.6, indoor="outdoor")
    b = _scored("Кофе", 51.0912, 71.4123, "cafe", (30, 45), 0.7)
    c = _scored("Музей", 51.0890, 71.4160, "museum", (60, 90), 0.9)   # too long for a micro-route
    d = _scored("Кофе2", 51.0913, 71.4124, "cafe", (30, 45), 0.65, window_end=AT + timedelta(minutes=10))  # closes
    r = build_route([c, b, a, d], anchor, AT, AT + timedelta(minutes=60))
    assert r is not None and r.total_min <= 60
    names = [s.title for s in r.stops]
    assert "Музей" not in names and "Кофе2" not in names and set(names) == {"Арт", "Кофе"}
    cold = W.get(0, 0, AT, FIX / "weather_cold.json")
    long_outdoor = _scored("Парк", 51.0950, 71.4300, "park", (30, 60), 0.9, indoor="outdoor")
    r2 = build_route([long_outdoor, b], anchor, AT, AT + timedelta(minutes=90), weather=cold)
    assert r2 is None  # 30+ minutes outside at −24 with wind is not a walk


# ---------- privacy ----------
def test_geohash_coarsening():
    assert geohash(51.0900, 71.4165, 7)[:4] == geohash(51.0901, 71.4166, 7)[:4]
    lat, lon, gh = coarse(51.090012, 71.416534)
    assert len(gh) == 7 and abs(lat - 51.090012) < 0.001 and abs(lon - 71.416534) < 0.001
    assert cell_center(gh) == pytest.approx((lat, lon), abs=1e-5)
