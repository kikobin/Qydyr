"""'Спросить про место' — questions about one place, answered only from what the system knows.

Like the AI helper in 2GIS place cards, but honest: every answer is "да / вероятно / неизвестно / нет"
with the evidence it rests on. Hours, events and the request needs (кофе, розетки, дети, коляска,
тихо...) are answered by rules; anything else goes to Claude with the place's facts and an order
to say "не знаю" when the facts don't cover it.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..badges import describe_event, describe_place
from . import knowledge as K
from .intent import _NEED_RE
from .recommend import cand_from_place

log = logging.getLogger(__name__)

Certainty = Literal["yes", "likely", "unknown", "no"]
CERT_WORD = {"yes": "да", "likely": "вероятно", "unknown": "неизвестно", "no": "нет"}
HOURS_RE = re.compile(r"до скольки|во сколько|закрыва|открыт|откро|работает|часы|график|режим|open|close|hours|"
                      r"жұмыс", re.I)
EVENTS_RE = re.compile(r"что (там )?(идет|идёт|сейчас)|событи|выставк|концерт|афиш|спектакл|what.?s on|event", re.I)
PRICE_RE = re.compile(r"бесплатн|цена|цены|стоит|стоимост|билет|вход\b|чек|дорого|price|ticket|free|тегін|баға", re.I)
WHERE_RE = re.compile(r"как найти|где (?:это|находится|вход)|какой этаж|на каком этаже|этаж|вход|остановк|"
                      r"как доехать|where is|which floor|entrance|қай қабат", re.I)
RATING_RE = re.compile(r"рейтинг|отзыв|оценк|хвалят|rating|review|пікір", re.I)


class QAAnswer(BaseModel):
    answer: str
    certainty: Certainty
    evidence: list[str] = Field(default_factory=list)
    source: Literal["data", "rules", "claude"] = "rules"


class LLMQA(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: str = Field(description="short answer in the question's language, or that the facts don't say")
    certainty: Literal["yes", "likely", "unknown", "no"]
    evidence: str = Field(description='the exact fact used, copied from FACTS, or ""')


SYSTEM = ("Ты отвечаешь на вопрос о конкретном месте в Астане ТОЛЬКО по фактам из блока FACTS. "
          "Если в фактах нет ответа — certainty=unknown и честно скажи, что в данных этого нет, и что стоит уточнить "
          "на месте. Ничего не придумывай: ни цен, ни часов, ни услуг. evidence — дословно факт из FACTS.")


def facts_of(row: dict, events: list[dict]) -> dict:
    attrs = {k: v for k, v in (row.get("attrs") or {}).items() if v not in (None, "", [], {})}
    return {"name": row.get("name"), "category": row.get("category"), "subcategory": row.get("subcategory"),
            "indoor": row.get("indoor"), "opening_hours": row.get("opening_hours"), "now": describe_place(row),
            "attrs": attrs, "events": [{"title": e["title"], "when": describe_event(e)} for e in events]}


def answer(row: dict, events: list[dict], question: str, at: datetime, llm=None) -> QAAnswer:
    q = question.strip()
    parts: list[QAAnswer] = []
    a = row.get("attrs") or {}
    if HOURS_RE.search(q):
        oh = row.get("opening_hours")
        now = describe_place(row)
        src = "2ГИС" if a.get("opening_hours:source") else "OpenStreetMap"
        note = f" {a['hours_note'].capitalize()}." if a.get("hours_note") else ""   # "касса до 20:30"
        if row.get("open_state") == "unknown":
            parts.append(QAAnswer(answer="Часы работы в данных не указаны — уточните перед выходом.",
                                  certainty="unknown", evidence=[f"{src}: opening_hours={oh}"] if oh else []))
        else:
            parts.append(QAAnswer(answer=f"Сейчас {now}.{note}", certainty="yes", source="data",
                                  evidence=[f"{src}: opening_hours={oh}"] if oh else []))
    if WHERE_RE.search(q) and not PRICE_RE.search(q.replace("вход", "")):
        inside = ", ".join(x for x in (a.get("in_building"), a.get("where_inside")) if x)
        st = a.get("nearest_station") or {}
        said = ([f"Где: {inside}."] if inside else []) + ([f"Рядом остановка «{st['name']}», {st['m']} м."]
                                                         if st.get("name") and st.get("m") is not None else [])
        if said or row.get("address"):
            parts.append(QAAnswer(answer=" ".join(said) or f"Адрес: {row['address']}.", certainty="yes",
                                  source="data", evidence=[a.get("2gis:url") or "OpenStreetMap"]))
    if RATING_RE.search(q):
        if a.get("2gis:rating") is not None and a.get("2gis:reviews"):
            r = f"{float(a['2gis:rating']):.1f}".replace(".", ",")
            parts.append(QAAnswer(answer=f"Рейтинг в 2ГИС {r} — {a['2gis:reviews']} отзывов.", certainty="yes",
                                  source="data", evidence=[a.get("2gis:url") or "2ГИС"]))
        else:
            parts.append(QAAnswer(answer="Рейтинга в моих данных нет.", certainty="unknown"))
    if EVENTS_RE.search(q):
        if events:
            ev = "; ".join(f"{e['title']} — {describe_event(e)}" for e in events[:3])
            parts.append(QAAnswer(answer=f"Там: {ev}.", certainty="yes", source="data",
                                  evidence=[e.get("url") or "" for e in events[:3] if e.get("url")]))
        else:
            parts.append(QAAnswer(answer="В афишах, которые я читаю, событий здесь на ближайшие часы нет.",
                                  certainty="unknown"))
    if PRICE_RE.search(q):
        fee = (row.get("attrs") or {}).get("fee")
        free_ev = [e for e in events if e.get("is_free")]
        if free_ev:
            parts.append(QAAnswer(answer=f"Вход на «{free_ev[0]['title']}» бесплатный (по афише).", certainty="yes",
                                  source="data", evidence=[free_ev[0].get("url") or "афиша"]))
        elif fee == "no":
            parts.append(QAAnswer(answer="Вход свободный.", certainty="yes", source="data",
                                  evidence=["OpenStreetMap: fee=no"]))
        elif fee == "yes":
            parts.append(QAAnswer(answer="Вход платный, цену в данных не нашёл.", certainty="yes", source="data",
                                  evidence=["OpenStreetMap: fee=yes"]))
        elif any(e.get("price_text") for e in events):
            e = next(e for e in events if e.get("price_text"))
            parts.append(QAAnswer(answer=f"По афише: {e['price_text']}.", certainty="yes", source="data",
                                  evidence=[e.get("url") or "афиша"]))
        elif a.get("avg_check"):
            lunch = " Есть бизнес-ланч." if a.get("business_lunch") == "yes" else ""
            check = f"{int(a['avg_check']):,}".replace(",", " ")
            parts.append(QAAnswer(answer=f"Средний чек около {check} ₸ (2ГИС).{lunch}", certainty="yes", source="data",
                                  evidence=[a.get("2gis:url") or "2ГИС"]))
        else:
            parts.append(QAAnswer(answer="Цены в данных нет — уточните на месте.", certainty="unknown"))

    cand = cand_from_place(row)
    cand.attached = events
    needs = [n for n in K.NEED_LABELS if _NEED_RE[n].search(q.lower())]
    for n in needs:
        if n in ("event",) and EVENTS_RE.search(q):
            continue
        if n == "free" and PRICE_RE.search(q):
            continue
        cert, evd = K.need_match(cand, n, at=at)
        text = f"{K.NEED_LABELS[n].capitalize()}: {CERT_WORD[cert]}" + (f" — {evd}" if evd else "")
        if cert == "unknown":
            text += ". В данных этого нет, лучше уточнить на месте"
        parts.append(QAAnswer(answer=text + ".", certainty=cert, evidence=[evd] if evd else []))  # type: ignore[arg-type]

    if parts:
        order = ["no", "unknown", "likely", "yes"]
        worst = min((p.certainty for p in parts), key=order.index) if len(parts) > 1 else parts[0].certainty
        return QAAnswer(answer=" ".join(p.answer for p in parts), certainty=worst,
                        evidence=[e for p in parts for e in p.evidence if e],
                        source="data" if all(p.source == "data" for p in parts) else "rules")
    if llm is not None:
        try:
            got = llm.structured(system=SYSTEM, schema=LLMQA, max_tokens=600,
                                 user=f"FACTS:\n{json.dumps(facts_of(row, events), ensure_ascii=False, default=str)}"
                                      f"\n\nВОПРОС: {q}")
            return QAAnswer(answer=got.answer, certainty=got.certainty, evidence=[got.evidence] if got.evidence else [],
                            source="claude")
        except Exception as e:  # the LLM is optional
            log.warning("place QA via Claude failed: %s", e)
    return QAAnswer(answer="В данных об этом месте ответа нет — уточните на месте. Могу подсказать часы работы, "
                           "события, вход и средний чек, этаж и вход, рейтинг, кофе, розетки, Wi‑Fi, детей, "
                           "собак и доступность для колясок.",
                    certainty="unknown")
