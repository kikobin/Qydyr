"""Answer in the person's language. The answer is built in Russian from facts, then translated by
Claude (Sonnet 5.5) with strict instructions: keep names, numbers, times and links as they are.

Two shapes: a whole text (the bot, the CLI) and the strings inside a structured answer (the Mini
App renders fields, not text). Translated strings are cached in memory: statuses like
"открыто до 22:00" repeat a lot."""
from __future__ import annotations

import logging
import re
from collections import OrderedDict
from typing import Any

from pydantic import BaseModel, ConfigDict

from .. import settings

log = logging.getLogger(__name__)
LANG_NAMES = {"kk": "казахский", "en": "английский"}
SYSTEM = ("Переведи ответ городского гида на {lang} язык. Сохрани без изменений: названия мест, числа, время, "
          "расстояния, ссылки и разбивку на строки. Ничего не добавляй и не убирай. Верни только перевод.")
SYSTEM_LIST = ("Переведи каждую строку интерфейса городского гида по Астане на {lang} язык. Названия мест в "
               "кавычках «…», числа, время и единицы оставь как есть. Верни столько же строк в том же порядке.")
_CACHE: "OrderedDict[tuple[str, str], str]" = OrderedDict()
_CACHE_MAX = 5000


class Strings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[str]


def _llm(llm):
    if llm is not None:
        return llm
    if not settings.ANTHROPIC_API_KEY:
        return None
    from ..llm import Claude
    return Claude(settings.CLAUDE_MODEL_SMART, fast=True)


def translate(text: str, lang: str, llm=None) -> str | None:
    """Translated text, or None if translation is unavailable (the caller keeps Russian)."""
    if lang not in LANG_NAMES:
        return None
    try:
        llm = _llm(llm)
        if llm is None:
            return None
        return llm.text(system=SYSTEM.format(lang=LANG_NAMES[lang]), user=text) or None
    except Exception as e:
        log.warning("translation failed: %s", e)
        return None


def translate_strings(strings: list[str], lang: str, llm=None) -> list[str] | None:
    if lang not in LANG_NAMES:
        return None
    todo = [s for s in dict.fromkeys(strings) if s and (lang, s) not in _CACHE]
    if todo:
        try:
            llm = _llm(llm)
            if llm is None:
                return None
            got = llm.structured(system=SYSTEM_LIST.format(lang=LANG_NAMES[lang]), schema=Strings,
                                 user="\n".join(f"{i + 1}. {s}" for i, s in enumerate(todo)), max_tokens=8000)
            if len(got.items) != len(todo):
                raise ValueError(f"expected {len(todo)} strings, got {len(got.items)}")
            for i, (src, dst) in enumerate(zip(todo, got.items), 1):
                _CACHE[(lang, src)] = re.sub(rf"^\s*{i}\.\s+", "", dst)  # the model may echo the numbering
                if len(_CACHE) > _CACHE_MAX:
                    _CACHE.popitem(last=False)
        except Exception as e:
            log.warning("translation of answer fields failed: %s", e)
            return None
    return [_CACHE.get((lang, s), s) if s else s for s in strings]


# which fields of Answer.model_dump() are human text (titles of places are names: kept as is)
_PATHS = [("notes", None), ("group_notes", None), ("weather", "s"), ("items", "status"), ("items", "why"), ("items", "pitch"),
          ("items", "where"),
          ("items", "what_to_do"), ("items", "warnings"), ("items", "group_fit"), ("rejected", "reason"),
          ("chips", "label"), ("funnel", "label")]


def translate_answer(ans: dict[str, Any], lang: str, llm=None) -> bool:
    """Translate the text fields of a dumped Answer in place. True if done."""
    slots: list[tuple[Any, Any]] = []   # (container, key)

    def add(container, key):
        if isinstance(container.get(key) if isinstance(container, dict) else container[key], str):
            slots.append((container, key))

    for top, sub in _PATHS:
        v = ans.get(top)
        if v is None:
            continue
        if sub == "s":
            add(ans, top)
        elif sub is None:
            for i in range(len(v)):
                add(v, i)
        else:
            for obj in v:
                x = obj.get(sub)
                if isinstance(x, list):
                    for i in range(len(x)):
                        add(x, i)
                else:
                    add(obj, sub)
    for chip in ans.get("chips") or []:
        for opt in chip.get("options") or []:
            add(opt, "label")
    ctx = ans.get("context") or {}
    if ctx.get("comfort"):
        add(ctx["comfort"], "label")
        for i in range(len(ctx["comfort"].get("factors") or [])):
            add(ctx["comfort"]["factors"], i)
    strings = [c[k] for c, k in slots]
    out = translate_strings(strings, lang, llm)
    if out is None:
        return False
    for (c, k), s in zip(slots, out):
        c[k] = s
    return True
