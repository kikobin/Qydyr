"""RAG, the generation step. The algorithm has already retrieved and ranked 1–3 places from the database
(distance, hours, events, weather); Claude turns each one's facts into one human sentence — why this, for
you, now — and one concrete thing to do there. It sees only those facts: a number or a time in its text
that is not in them throws the text away, and the template stays. Generated text is marked as such."""
from __future__ import annotations

import json
import logging
import re
from collections import OrderedDict
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from .. import settings
from .models import Answer
from .story import FACT_TAGS, SUB_RU

if TYPE_CHECKING:
    from .recommend import Scored

log = logging.getLogger(__name__)

SYSTEM = (
    "Ты пишешь объяснения для городского гида Qydyr по Астане. Алгоритм уже выбрал места по базе данных; "
    "тебе дают JSON: запрос человека, контекст (время, погода, сколько у него времени) и для каждого места "
    "факты из базы (OpenStreetMap, афиши) с номером n.\n"
    "Для каждого места верни:\n"
    "- why: одно короткое предложение до 110 знаков — чем это место отвечает на просьбу человека сейчас "
    "(тихо, с ребёнком, до вылета, ночью, погулять, в холод). Не повторяй расстояние, время в пути, часы "
    "работы и предупреждения: они показаны рядом. Опирайся ТОЛЬКО на факты из JSON. Не придумывай меню, цены, "
    "рейтинги, историю, интерьер, «уютно», «популярно», «лучший». Если факт помечен «вероятно», так и скажи "
    "на языке ответа («скорее всего», «likely», «мүмкін»). Факты в JSON по-русски: переводи их смысл, а "
    "русские слова в ответ на другом языке не вставляй.\n"
    "- todo: если todo_needed=true — одно конкретное действие на месте до 90 знаков, которое укладывается "
    "в visit_min (что сделать; куда пойти потом, если есть facts.then). Это идея, но без выдуманных деталей "
    "места: не называй блюда, экспонаты, залы, этажи; не советуй уточнять часы. Если todo_needed=false — "
    "пустая строка.\n"
    "Ничего не добавляй о месте сверх JSON: ни видов, ни набережных, ни «в центре города», ни «памятник», "
    "если kind другой, ни того, с кем человек идёт, если этого нет в request.\n"
    "Пиши на языке context.language (ru — по-русски, kk — по-казахски, en — по-английски), на «вы», "
    "спокойно и конкретно: без восклицательных знаков, эмодзи, канцелярита, без "
    "названия места в начале предложения.")
_NUM = re.compile(r"\d+")
_CACHE: "OrderedDict[str, dict[int, tuple[str, str]]]" = OrderedDict()


class Pitch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    n: int
    why: str
    todo: str = ""


class Pitches(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[Pitch]


def _facts(n: int, it, s: "Scored | None") -> dict:
    c = s.cand if s else None
    attrs = (c.attrs if c else {}) or {}
    walk = "прямо здесь" if it.walk_min == 0 else f"{it.walk_min} мин {'на такси' if it.by == 'taxi' else 'пешком'}"
    out = {
        "n": n, "name": it.title, "kind": SUB_RU.get(it.subcategory or "", it.subcategory or it.category),
        "how_far": f"{walk}, {it.distance_m} м", "status": it.status, "visit_min": list(it.visit_min),
        "matches": it.why, "warnings": it.warnings, "indoor": it.indoor,
        "tags": {k: attrs[k] for k in (*FACT_TAGS, "internet_access", "outdoor_seating") if attrs.get(k) not in (None, "")},
    }
    if it.attached_event:
        out["event_now_or_soon"] = it.attached_event
    if it.what_to_do_source == "data":
        out["from_listing"] = it.what_to_do
    elif "затем" in it.what_to_do:          # "затем кофе рядом — Starbucks (1 мин)": a real next stop
        out["then"] = it.what_to_do.split("затем", 1)[1].strip()
    out["todo_needed"] = it.what_to_do_source != "data"
    return out


def _context(ans: Answer, lang: str) -> dict:
    it = ans.intent
    lo, hi = it.budget_min, it.budget_max
    return {
        "language": lang, "request": it.text, "time": ans.at.strftime("%H:%M"), "where": ans.anchor.label if ans.anchor else None,
        "free_time_min": [lo, hi] if lo != hi else hi, "with_kids": it.with_kids, "group_size": it.group_size,
        "persona": it.persona, "deadline": it.deadline, "weather": ans.weather,
        "dark": bool(ans.context.weather and ans.context.weather.dark),
    }


def grounded(text: str, source: str) -> bool:
    """Every number in the generated text appears in the facts it was given."""
    have = set(_NUM.findall(source))
    return all(n in have for n in _NUM.findall(text))


class Narrator:
    def __init__(self, llm=None) -> None:
        self.llm = llm

    def _client(self):
        if self.llm is None:
            from anthropic import Anthropic

            from ..llm import Claude
            # a slow answer is worse than a template one: short timeout, no retries
            self.llm = Claude(settings.CLAUDE_MODEL_NARRATE, fast=True,
                              client=Anthropic(api_key=settings.ANTHROPIC_API_KEY, timeout=8.0, max_retries=0))
        return self.llm

    def __call__(self, ans: Answer, picked: "list[Scored]", lang: str = "ru") -> int:
        """Fill `pitch` (and the assistant's idea in `what_to_do`) of the answer's items, written straight in
        the person's language. Returns how many."""
        if not ans.items:
            return 0
        facts = [_facts(i + 1, it, picked[i] if i < len(picked) else None) for i, it in enumerate(ans.items)]
        payload = json.dumps({"context": _context(ans, lang), "places": facts}, ensure_ascii=False, default=str)
        got = _CACHE.get(payload)
        if got is None:
            try:
                out = self._client().structured(system=SYSTEM, user=payload, schema=Pitches, max_tokens=1200)
            except Exception as e:  # the template answer is a complete answer
                log.warning("narration failed: %s", e)
                return 0
            got = {p.n: (p.why.strip(), p.todo.strip()) for p in out.items}
            _CACHE[payload] = got
            if len(_CACHE) > 500:
                _CACHE.popitem(last=False)
        done = 0
        for i, it in enumerate(ans.items, 1):
            why, todo = got.get(i, ("", ""))
            if why and len(why) <= 220 and grounded(why, payload):
                it.pitch = why
                done += 1
            if todo and it.what_to_do_source == "idea" and len(todo) <= 180 and grounded(todo, payload):
                it.what_to_do = todo
        ans.narrated = done > 0
        if done:
            ans.used_data.append("текст «почему»: Claude по найденным данным, без новых фактов")
        return done
