"""RAG generation: Claude words the retrieved facts of the picked places, and only them."""
import json
from datetime import datetime

import pytest

from qydyr_data import settings
from qydyr_data.assistant import narrate
from qydyr_data.assistant.narrate import Narrator, Pitch, Pitches, grounded
from qydyr_data.assistant.recommend import Assistant, DbData
from qydyr_data.assistant.render import render
from qydyr_data.llm import Claude

from .conftest import FIX
from .test_pipeline_db import load_everything

AT = datetime(2026, 10, 23, 15, 0, tzinfo=settings.TZ)
EXPO = (51.0900, 71.4165)


@pytest.fixture(autouse=True)
def fresh_cache():
    narrate._CACHE.clear()
    yield
    narrate._CACHE.clear()


class FakeLLM:
    """Writes one sentence per place from what it was given; `bad` adds a number that is not in the facts."""
    def __init__(self, bad: int | None = None, fail: bool = False):
        self.bad, self.fail, self.calls = bad, fail, []

    def structured(self, *, system, user, schema, max_tokens=0):
        self.calls.append(json.loads(user))
        if self.fail:
            raise TimeoutError("slow")
        places = self.calls[-1]["places"]
        return Pitches(items=[Pitch(n=p["n"], why=f"Подходит под просьбу: {p['kind']}" + (", 4,9 звезды" if p["n"] == self.bad else ""),
                                    todo="Возьмите напиток и сядьте у окна" if p["todo_needed"] else "") for p in places])


def ask(db, sample_places, llm):
    load_everything(db, sample_places)
    return Assistant(DbData(db), narrator=Narrator(llm)).ask("хочу кофе и тихо", lat=EXPO[0], lon=EXPO[1], at=AT,
                                                             weather_file=FIX / "weather_mild.json")


def test_pitch_comes_from_the_facts_and_is_labelled(db, sample_places):
    llm = FakeLLM()
    ans = ask(db, sample_places, llm)
    sent = llm.calls[0]
    assert sent["context"]["request"] == "хочу кофе и тихо" and len(sent["places"]) == len(ans.items)
    assert all({"name", "status", "matches", "visit_min"} <= set(p) for p in sent["places"])
    assert ans.narrated and all(i.pitch.startswith("Подходит под просьбу") for i in ans.items)
    assert any("Claude по найденным данным" in u for u in ans.used_data)
    assert "текст: Claude по этим данным" in render(ans)


def test_a_number_that_is_not_in_the_facts_drops_the_sentence(db, sample_places):
    ans = ask(db, sample_places, FakeLLM(bad=1))
    assert ans.items[0].pitch is None and ans.items[0].why          # the template stays
    assert grounded("до 22:00, 6 мин", "открыто до 22:00 · 6 мин") and not grounded("рейтинг 4,9", "{}")


def test_claude_down_means_the_template_answer(db, sample_places):
    ans = ask(db, sample_places, FakeLLM(fail=True))
    assert ans.items and not ans.narrated and all(i.pitch is None and i.why for i in ans.items)


def test_fast_calls_turn_thinking_off_the_way_each_model_accepts():
    assert Claude("claude-haiku-5-5", client=object(), fast=True)._thinking() == {"thinking": {"type": "disabled"}}
    assert Claude("claude-sonnet-5-5", client=object(), fast=True)._thinking() == {"thinking": {"type": "between_tools"}}
    assert Claude("claude-opus-5-5", client=object(), fast=True)._thinking() == {}
    assert Claude("claude-haiku-5-5", client=object())._thinking() == {}


def test_api_words_the_answer_in_the_persons_language_while_the_rest_is_translated(db, sample_places, monkeypatch):
    from fastapi.testclient import TestClient

    from qydyr_data import api
    load_everything(db, sample_places)
    llm = FakeLLM()
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", None)
    monkeypatch.setattr(api, "_narrator", lambda: Narrator(llm))
    translated = []
    monkeypatch.setattr(api, "translate_answer", lambda dump, lang: translated.append(lang) or True)
    api.app.dependency_overrides[api.get_data] = lambda: DbData(db)
    try:
        r = TestClient(api.app).post("/ask", json={"text": "coffee and quiet", "lat": EXPO[0], "lon": EXPO[1],
                                                   "at": "2026-10-23T15:00:00", "lang": "en", "format": "structured"}).json()
    finally:
        api.app.dependency_overrides.clear()
    assert translated == ["en"] and llm.calls[0]["context"]["language"] == "en"
    assert r["lang"] == "en" and r["answer"]["narrated"] and all(i["pitch"] for i in r["answer"]["items"])
