"""Claude API layer without the network: a fake client stands in for anthropic.Anthropic."""
import json
from datetime import date
from types import SimpleNamespace

from anthropic import transform_schema

from qydyr_data.assistant.intent import ClaudeIntentParser, LLMIntent, understand
from qydyr_data.assistant.translate import translate
from qydyr_data.events.llm import ClaudeExtractor, COutput
from qydyr_data.llm import Claude


class FakeMessages:
    def __init__(self, parsed=None, text="перевод"):
        self.parsed, self.text_out, self.calls = parsed, text, []

    def parse(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(parsed_output=self.parsed, stop_reason="end_turn")

    def create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.text_out)])


def fake(parsed=None, text="перевод", **kw) -> tuple[Claude, FakeMessages]:
    msgs = FakeMessages(parsed, text)
    return Claude("claude-haiku-5-5", client=SimpleNamespace(messages=msgs), **kw), msgs


def _walk(schema):
    if isinstance(schema, dict):
        yield schema
        for v in schema.values():
            yield from _walk(v)
    elif isinstance(schema, list):
        for v in schema:
            yield from _walk(v)


def test_schemas_fit_structured_output_limits():
    for model in (COutput, LLMIntent):
        s = transform_schema(model)
        nodes = list(_walk(s))
        assert not any("anyOf" in n or isinstance(n.get("type"), list) for n in nodes)  # no unions
        for n in nodes:
            if n.get("type") == "object":
                assert n.get("additionalProperties") is False
                assert set(n.get("required", [])) == set(n.get("properties", {}))      # nothing optional


EVENT = {
    "title": "Вечерняя выставка «Ночь в музее»", "kind": "exhibition",
    "venue_name": "Национальный музей Республики Казахстан", "address": "", "date_start": "2026-10-23",
    "date_end": "", "time_start": "18:00", "time_end": "22:00", "sessions": [], "schedule_opening_hours": "",
    "price_text": "", "price_min": "", "is_free": "yes", "age_limit": "", "description": "Залы золота вечером.",
    "status": "scheduled", "evidence": "Когда: 23 октября, с 18:00 до 22:00",
}


def test_extractor_converts_and_caches(tmp_path):
    llm, msgs = fake(COutput.model_validate({"events": [EVENT, {**EVENT, "title": "  "}]}), cache_dir=tmp_path)
    ex = ClaudeExtractor(llm)
    got = ex.extract("Когда: 23 октября, с 18:00 до 22:00", mode="page", ref_date=date(2026, 10, 20),
                     title_hint="Ночь в музее", url="https://x")
    assert len(got) == 1  # an event without a title is dropped
    e = got[0]
    assert (e.date_start, e.date_end, e.time_start, e.address, e.is_free, e.price_min) == (
        "2026-10-23", None, "18:00", None, True, None)
    call = msgs.calls[0]
    assert call["model"] == "claude-haiku-5-5" and call["output_format"] is COutput
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "2026-10-20 (вторник)" in call["system"][0]["text"]
    # same page again: served from cache, no second API call
    ex.extract("Когда: 23 октября, с 18:00 до 22:00", mode="page", ref_date=date(2026, 10, 20),
               title_hint="Ночь в музее", url="https://x")
    assert len(msgs.calls) == 1


def test_intent_parser_adds_to_rules_never_replaces():
    llm, _ = fake(LLMIntent(anchor_text="Хан Шатыр", budget_min=0, budget_max=0, needs=["indoor", "free"],
                            with_kids=True, wheelchair=False, persona="", deadline_flight=False, group_size=1))
    it = understand("хочу куда-нибудь погреться и не тратить деньги, ребёнку 4, мы у Хан Шатыра 30 минут",
                    llm=ClaudeIntentParser(llm))
    assert it.budget_max == 30                       # rules found it; the LLM's 0 doesn't override
    assert it.with_kids and "kids" in it.needs and {"indoor", "free"} <= set(it.needs)
    assert it.anchor_text


def test_intent_parser_failure_falls_back_to_rules():
    class Broken:
        def structured(self, **kw):
            raise RuntimeError("API down")
    it = understand("кофе у Mega Silk Way", llm=ClaudeIntentParser(Broken()))
    assert "coffee" in it.needs  # the assistant keeps working without the LLM


def test_translate_uses_claude_text():
    llm, msgs = fake(text="Жақын жерде: «Тихий угол» — 5 мин")
    assert translate("Рядом: «Тихий угол» — 5 мин", "kk", llm=llm).startswith("Жақын")
    assert "казахский" in msgs.calls[0]["system"]
    assert translate("x", "ru", llm=llm) is None


def test_no_key_means_no_llm(monkeypatch):
    from qydyr_data import settings
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", None)
    assert translate("текст", "en") is None
    try:
        Claude()
    except RuntimeError as e:
        assert "ANTHROPIC_API_KEY" in str(e)
    else:
        raise AssertionError("expected RuntimeError")
    assert json.dumps({"ok": True})
