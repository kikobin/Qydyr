"""End to end on a real PostGIS: afisha + Telegram fixtures -> DB -> "what is on near me right now"."""
from datetime import datetime

from qydyr_data import demo, settings, store
from qydyr_data.badges import describe_event, describe_place

from .conftest import fixture_text

TZ = settings.TZ
MUSEUM = (51.1186, 71.4701)
EXPO = (51.0900, 71.4165)
EVENT_RE = r"^https://sxodim\.com/astana/event/[a-z0-9-]+/?$"

PAGES = {u: fixture_text(f) for u, f in demo.PAGES.items()}
LLM_OUTPUTS = demo.LLM_OUTPUTS
SOURCES = demo.SOURCES


def at(day: int, hh: int, mm: int = 0) -> datetime:
    return datetime(2026, 10, day, hh, mm, tzinfo=TZ)


def load_everything(db, sample_places):
    return demo.load(db, sample_places, PAGES)


def test_pipeline_end_to_end(db, sample_places):
    raws, records, stats, extractor = load_everything(db, sample_places)

    # collection: 2 events via JSON-LD, 1 via LLM page, 1 via Telegram; nothing from the related-events block
    assert sorted(r.extraction for r in raws) == ["jsonld", "jsonld", "llm", "telegram"]
    assert all(not s.errors for s in stats)
    page_call = next(c for c in extractor.calls if c["mode"] == "page")
    assert "Концерт «Осень»" not in page_call["text"] and "Когда: 23 октября" in page_call["text"]
    assert all(r.evidence_ok for r in raws)

    by_title = {r.title: r for r in records}
    exhibition = by_title["Выставка «Свет Великой степи»"]
    assert exhibition.place_id == "osm:node/1001" and exhibition.is_temporary
    assert {s.hours_from for s in exhibition.sessions} == {"venue"}
    assert by_title["Концерт «Осенние мелодии»"].status == "cancelled"
    assert by_title["Джаз-вечер"].place_id == "osm:node/1007"


def test_whats_on_evening_exhibition_now(db, sample_places):
    load_everything(db, sample_places)
    rows = store.whats_on(db, *MUSEUM, radius_m=1500, at=at(23, 19, 10))
    assert [r["title"] for r in rows] == ["Вечерняя выставка «Ночь в музее»"]  # the day exhibition closed at 18:00
    r = rows[0]
    assert r["state"] == "now" and r["minutes_left"] == 170
    assert {"evening", "today_only", "free"} <= set(r["labels"])
    assert describe_event(r) == "Идёт сейчас · только сегодня · до 22:00 · бесплатно"  # the title already says "вечерняя выставка"
    assert describe_event(r, title="Ночь в музее") == "Идёт сейчас · вечерняя выставка · только сегодня · до 22:00 · бесплатно"


def test_whats_on_temporary_exhibition_last_days(db, sample_places):
    load_everything(db, sample_places)
    rows = store.whats_on(db, *MUSEUM, radius_m=1500, at=at(23, 12))
    assert [r["state"] for r in rows] == ["now", "later_today"]
    ex = rows[0]
    assert ex["title"] == "Выставка «Свет Великой степи»" and ex["days_left"] == 3
    assert {"temporary", "last_days", "venue_hours"} <= set(ex["labels"])
    assert describe_event(ex) == ("Идёт сейчас · временная выставка · до 18:00 · последние дни (до 26 октября)"
                                  " · время по графику площадки")
    assert describe_event(rows[1]).startswith("Сегодня в 18:00 · только сегодня")


def test_whats_on_soon_and_cancelled_hidden(db, sample_places):
    load_everything(db, sample_places)
    rows = store.whats_on(db, *EXPO, radius_m=1500, at=at(23, 19, 10))
    assert [r["title"] for r in rows] == ["Джаз-вечер"]
    assert rows[0]["state"] == "soon" and rows[0]["minutes_to_start"] == 50
    assert {"evening", "starting_soon", "end_estimated"} <= set(rows[0]["labels"])
    opera = store.whats_on(db, 51.1307, 71.4146, radius_m=500, at=at(23, 18, 30))
    assert opera == []  # the concert was cancelled


def test_places_open_state(db, sample_places):
    load_everything(db, sample_places)
    rows = {r["name"]: r for r in store.places_near(db, *MUSEUM, radius_m=600, at=at(23, 19, 10))}
    museum = rows["Национальный музей Республики Казахстан"]
    assert museum["open_state"] == "closed" and museum["minutes_to_open"] == 890
    assert describe_place(museum) == "закрыто, откроется 24 октября в 10:00"
    assert describe_place(rows["Кафе «Коктем»"]) == "открыто до 23:00"
    assert rows["Детская площадка"]["open_state"] == "unknown"


def test_reload_is_idempotent(db, sample_places):
    load_everything(db, sample_places)
    load_everything(db, sample_places)
    n = db.execute("select count(*) n, max(jsonb_array_length(sources)) s from events").fetchone()
    assert n == {"n": 4, "s": 1}
    st = store.stats(db)
    assert st["places"]["n"] == 21 and st["events"]["cancelled"] == 1
