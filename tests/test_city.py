"""Qydyr for the city: the supply audit (where typical requests find nothing nearby), live demand as counts
only (k-anonymous, chip edits are not new people), and the data page."""
from datetime import date

from fastapi.testclient import TestClient

from qydyr_data import api, city, settings, store
from qydyr_data.api import app, get_data
from qydyr_data.assistant.privacy import geohash
from qydyr_data.assistant.recommend import DbData

from .test_pipeline_db import load_everything

DAY = date(2026, 10, 19)   # a Monday: the audit asks for the week from here


def test_audit_asks_every_populated_cell_and_names_it(db, sample_places):
    load_everything(db, sample_places)
    gaps = city.run_audit(db, [city.BY_ID["work_quiet"], city.BY_ID["step_free"]], day=DAY)
    assert set(gaps) == {"work_quiet", "step_free"}
    rows = store.audit(db, "work_quiet")
    assert rows and all(r["places"] >= city.MIN_PLACES and r["at"].weekday() == 1 for r in rows)
    v = city.gaps_view(db, "step_free")
    assert v["summary"]["cells"] == len(rows) and v["scenario"]["request"].startswith("я на коляске")
    assert sum(v["summary"][k] for k in ("none", "guess", "far", "ok")) == len(rows)
    # nothing in the fixture city is marked step-free in OSM: a guess is never shown as covered
    assert all(c["level"] != "ok" or c["confirmed"] for c in v["cells"])
    assert any(c["label"] for c in v["cells"])


def test_levels():
    s, strict = city.BY_ID["work_quiet"], city.BY_ID["step_free"]
    assert city.level({"options": 0, "confirmed": 0, "nearest_min": None}, s) == "none"
    assert city.level({"options": 2, "confirmed": 0, "nearest_min": 5}, s) == "ok"
    assert city.level({"options": 2, "confirmed": 0, "nearest_min": 5}, strict) == "guess"
    assert city.level({"options": 1, "confirmed": 1, "nearest_min": 25}, strict) == "far"


def test_demand_is_counts_shown_from_k_and_chip_edits_dont_count(db, sample_places, monkeypatch):
    load_everything(db, sample_places)
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", None)
    app.dependency_overrides[get_data] = lambda: DbData(db)
    try:
        c = TestClient(app)
        body = {"text": "я на коляске, тихо поработать с розеткой", "lat": 51.09, "lon": 71.4165,
                "at": "2026-10-23T15:00:00", "llm": False}
        first = c.post("/ask", json=body).json()["answer"]
        assert c.get("/city/demand").json()["cells"] == []            # one person: not shown
        for _ in range(4):                                           # chip edits re-run the same request
            c.post(f"/ask/{first['id']}/patch", json={"patch": {"budget_max": 90}})
        assert c.get("/city/demand").json()["cells"] == []
        for _ in range(2):
            c.post("/ask", json=body)
        d = c.get("/city/demand").json()
        cell = geohash(first["anchor"]["lat"], first["anchor"]["lon"], city.CELL_PRECISION)
        assert [x["cell"] for x in d["cells"]] == [cell] and d["k"] == store.DEMAND_K
        needs = {n["need"]: n for n in d["cells"][0]["needs"]}
        assert needs["socket"]["asked"] == 3 and needs["accessible"]["asked"] == 3
        row = db.execute("select * from demand limit 1").fetchone()
        assert set(row) == {"day", "cell", "need", "asked", "unmet"}  # no text, no coordinates, no user
    finally:
        app.dependency_overrides.clear()


def test_assumed_needs_are_not_demand():
    from qydyr_data.assistant.intent import understand
    it = understand("я у Байтерека, есть час")
    assert city.unmet_needs(it, [])[0] == []                         # "просто погулять" was our guess, not a wish
    it = understand("с ребёнком, час")
    assert city.unmet_needs(it, []) == (["kids"], ["kids"])


def test_city_api_and_data_page(db, sample_places):
    load_everything(db, sample_places)
    city.run_audit(db, [city.BY_ID["night"]], day=DAY)
    app.dependency_overrides[get_data] = lambda: DbData(db)
    try:
        c = TestClient(app)
        assert [s["id"] for s in c.get("/city/scenarios").json()] == [s.id for s in city.SCENARIOS]
        g = c.get("/city/gaps", params={"scenario": "night"}).json()
        assert g["cells"] and {"level", "label", "best"} <= set(g["cells"][0])
        assert c.get("/city/gaps", params={"scenario": "nope"}).status_code == 404
        d = c.get("/city/data").json()
        assert d["places"]["n"] > 0 and d["sources"][0]["id"] == "osm" and d["tours"]["facts"] > 0
        assert {"with_hours", "with_wheelchair"} <= set(d["places"])
        assert c.get("/app/city.html").status_code == 200
    finally:
        app.dependency_overrides.clear()
