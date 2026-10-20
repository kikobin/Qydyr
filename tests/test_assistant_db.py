"""The assistant end to end on PostGIS: the brief's scenarios and more, checked by the evaluator."""
from datetime import datetime

from fastapi.testclient import TestClient

from qydyr_data import settings
from qydyr_data.api import app, get_data
from qydyr_data.assistant.evaluate import run_file
from qydyr_data.assistant.recommend import Assistant, DbData
from qydyr_data.assistant.render import render

from .test_pipeline_db import load_everything

TZ = settings.TZ


def test_all_fixture_scenarios_pass(db, sample_places):
    load_everything(db, sample_places)
    results = run_file(db, settings.ROOT / "eval" / "scenarios_fixtures.yaml")
    failed = {r.id: r.failures for r in results if not r.ok}
    assert not failed, failed
    assert len(results) >= 11


def test_evening_exhibition_text(db, sample_places):
    load_everything(db, sample_places)
    ans = Assistant(DbData(db)).ask("Что интересного рядом прямо сейчас?", lat=51.1186, lon=71.4701,
                                    at=datetime(2026, 10, 23, 19, 10, tzinfo=TZ),
                                    weather_file=settings.ROOT / "tests/fixtures/weather_mild.json")
    text = render(ans)
    assert "Вечерняя выставка «Ночь в музее»: Идёт сейчас · только сегодня · до 22:00 · бесплатно" in text
    assert "Что делать (из афиши): Залы золота открыты вечером" in text  # what to do — from the afisha, marked as data
    assert "округлена до ~150 м, не сохраняется" in text      # GPS disclosure
    # exact coordinates never leave the assistant: only the ~150 m cell centre is returned
    assert (ans.anchor.lat, ans.anchor.lon) != (51.1186, 71.4701)
    assert abs(ans.anchor.lat - 51.1186) < 0.002 and abs(ans.anchor.lon - 71.4701) < 0.002


def test_api(db, sample_places):
    load_everything(db, sample_places)
    app.dependency_overrides[get_data] = lambda: DbData(db)
    try:
        client = TestClient(app)
        assert client.get("/health").json() == {"ok": True}
        r = client.post("/ask", json={"text": "Я у здании Экспо 2017, хочу что-то необычное на 1–2 часа",
                                      "at": "2026-10-23T15:00:00"})
        body = r.json()
        assert r.status_code == 200 and body["answer"]["items"][0]["title"] == "Нур Алем"
        assert "Нур Алем" in body["text"]
        w = client.get("/whats-on", params={"lat": 51.1186, "lon": 71.4701, "at": "2026-10-23T19:10:00"}).json()
        assert w[0]["badge"].startswith("Идёт сейчас")
        p = client.get("/places-near", params={"lat": 51.1186, "lon": 71.4701, "radius": 300,
                                               "at": "2026-10-23T19:10:00", "categories": "food"}).json()
        assert p[0]["badge"] == "открыто до 23:00"
    finally:
        app.dependency_overrides.clear()
