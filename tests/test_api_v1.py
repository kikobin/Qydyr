"""API v1: chips patches, why-not, walks, place card and Q&A, pulse, feedback, Telegram initData,
translation of structured answers."""
import time

from fastapi.testclient import TestClient

from qydyr_data import settings
from qydyr_data.api import app, get_data
from qydyr_data.assistant.recommend import DbData
from qydyr_data.assistant.translate import Strings, translate_answer
from qydyr_data.pulse import AnswerStore, Pulse
from qydyr_data.telegram_auth import sign_init_data, validate_init_data

from .test_pipeline_db import load_everything

AT = "2026-10-23T19:10:00"
EXPO = {"lat": 51.0900, "lon": 71.4165}


def client_for(db, sample_places, monkeypatch):
    load_everything(db, sample_places)
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", None)
    app.dependency_overrides[get_data] = lambda: DbData(db)
    return TestClient(app)


def test_ask_patch_and_why_not(db, sample_places, monkeypatch):
    c = client_for(db, sample_places, monkeypatch)
    try:
        cfg = c.get("/config").json()
        assert cfg["map_style"]["dark"].startswith("https://") and cfg["presets"][0]["id"] == "now"
        r = c.post("/ask", json={"text": "что интересного рядом", **EXPO, "at": AT}).json()
        a = r["answer"]
        assert a["id"] and a["funnel"][-1]["n"] == len(a["items"]) and "Почему не другое" in r["why_not_text"]
        assert a["anchor"]["lat"] != EXPO["lat"]          # exact GPS never comes back
        # tap the "только события" chip
        r2 = c.post(f"/ask/{a['id']}/patch", json={"patch": {"only_events": True}}).json()["answer"]
        assert {ch["key"]: ch for ch in r2["chips"]}["only_events"]["active"] is True
        assert len(r2["items"]) < len(a["items"])
        assert c.get(f"/answer/{r2['id']}").json()["id"] == r2["id"]
        assert c.post("/ask/nope/patch", json={"patch": {}}).status_code == 404
        # weather machine
        r3 = c.post("/ask", json={"text": "хочу прогуляться час", **EXPO, "at": "2026-10-23T15:00:00",
                                  "weather_preset": "blizzard", "format": "structured"}).json()
        assert "text" not in r3 and r3["answer"]["context"]["simulated"] == "метель −25"
        assert all(i["indoor"] != "outdoor" for i in r3["answer"]["items"])
        assert c.post("/ask", json={"text": "x", "weather_preset": "tornado"}).status_code == 422
        # surprise
        r4 = c.post("/ask", json={"surprise": True, **EXPO, "at": "2026-10-23T15:00:00", "seed": 3}).json()
        assert r4["answer"]["surprise"] and len(r4["answer"]["items"]) == 1
        assert c.post("/feedback", json={"answer_id": a["id"], "vote": "up"}).json() == {"ok": True}
        pulse = c.get("/pulse", params={"at": AT}).json()
        assert pulse["requests"] >= 3 and pulse["cells"] and pulse["counters"]["places"] == 21
        assert pulse["now_on"] and all("badge" in e for e in pulse["now_on"])
        assert "text" not in str(pulse["recent"])  # no request text in the pulse
    finally:
        app.dependency_overrides.clear()


def test_place_card_qa_and_walks(db, sample_places, monkeypatch):
    c = client_for(db, sample_places, monkeypatch)
    try:
        museum = next(p for p in sample_places if p.name.startswith("Национальный музей"))
        card = c.get(f"/place/{museum.id}", params={"at": AT}).json()
        assert card["badge"].startswith("закрыто") and card["events"][0]["badge"].startswith("Идёт сейчас")
        qa = c.post(f"/place/{museum.id}/ask", json={"q": "что там сейчас идёт?", "at": AT}).json()
        assert "Ночь в музее" in qa["answer"] and qa["certainty"] == "yes"
        assert c.get("/place/osm:node/0").status_code == 404
        walks = c.get("/walks", params={**EXPO, "at": "2026-10-23T15:00:00", "weather_preset": "rain"}).json()
        assert walks and all(w["answer"]["context"]["simulated"] == "дождь +8" for w in walks)
        en = c.get("/walks", params={**EXPO, "at": "2026-10-23T15:00:00", "lang": "en"}).json()
        assert en and "Warm route" in [w["title"] for w in en] and all("min" in w["meta"] for w in en)
    finally:
        app.dependency_overrides.clear()


def test_telegram_init_data(db, sample_places, monkeypatch):
    c = client_for(db, sample_places, monkeypatch)
    token = "123456:TEST-token"
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", token)
    try:
        good = sign_init_data({"auth_date": str(int(time.time())), "query_id": "AA",
                               "user": '{"id":1,"first_name":"Бекнур"}'}, token)
        assert validate_init_data(good, token)["user"]["first_name"] == "Бекнур"
        assert validate_init_data(good, "other:token") is None
        old = sign_init_data({"auth_date": "1000", "query_id": "AA"}, token)
        assert validate_init_data(old, token) is None
        assert c.get("/health", headers={"X-Telegram-Init-Data": good}).status_code == 200
        assert c.get("/health", headers={"X-Telegram-Init-Data": good.replace("AA", "AB")}).status_code == 401
        assert c.get("/health").status_code == 200   # a normal browser (the web demo) still works
    finally:
        app.dependency_overrides.clear()


def test_translate_answer_fields():
    calls = []

    class FakeLLM:
        def structured(self, *, system, user, schema, max_tokens):
            calls.append(user)
            lines = [line.split(". ", 1)[1] for line in user.splitlines()]
            return Strings(items=[f"{i + 1}. EN:{s}" for i, s in enumerate(lines)])

    ans = {"notes": ["Закат в 17:07"], "weather": "+11°", "items": [
        {"status": "открыто до 23:00", "why": ["прямо здесь"], "what_to_do": "возьмите кофе", "warnings": [],
         "group_fit": [], "title": "Кафе «Коктем»"}],
        "rejected": [{"reason": "закрыто"}], "chips": [{"label": "пешком", "options": [{"label": "на такси"}]}],
        "funnel": [{"label": "мест рядом"}], "context": {"comfort": {"label": "комфортно", "factors": ["темно"]}}}
    assert translate_answer(ans, "en", llm=FakeLLM())
    assert ans["items"][0]["status"] == "EN:открыто до 23:00" and ans["items"][0]["title"] == "Кафе «Коктем»"
    assert ans["chips"][0]["options"][0]["label"] == "EN:на такси" and ans["context"]["comfort"]["factors"] == ["EN:темно"]
    ans2 = {"notes": ["Закат в 17:07"]}
    assert translate_answer(ans2, "en", llm=FakeLLM()) and len(calls) == 1   # cached, no second call
    assert translate_answer({"notes": ["x"]}, "ru") is False


def test_pulse_and_answer_store_units():
    p = Pulse()
    now = time.time()
    p.record(51.09, 71.4165, ["coffee", "quiet"], "ru", 2, now=now - 60)
    p.record(51.0901, 71.4166, ["coffee"], "kk", 1, now=now - 30)
    p.record(51.16, 71.47, ["walk"], "ru", 3, now=now - 4 * 3600)    # outside the 3-hour window
    snap = p.snapshot(now=now)
    assert snap["requests"] == 2 and len(snap["cells"]) == 1 and snap["cells"][0]["n"] == 2
    assert snap["top_needs"][0] == {"need": "coffee", "n": 2} and snap["langs"] == {"ru": 1, "kk": 1}
    st = AnswerStore(maxlen=2)
    st.put("a", {"x": 1})
    st.put("b", {"x": 2})
    st.put("c", {"x": 3})
    assert st.get("a") is None and st.get("c") == {"x": 3}
