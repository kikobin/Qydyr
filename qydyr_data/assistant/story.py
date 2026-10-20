"""'Рассказать о месте' — a short spoken story for someone standing nearby (like AI audio guides),
built only from the facts the system has: OSM tags, the description, what is on there now.
With Claude it reads like a guide; without it, a plain factual summary. Either way it is marked
as the assistant's text, and it never invents dates, architects or legends."""
from __future__ import annotations

import json
import logging
from collections import OrderedDict

from ..badges import describe_event
from . import knowledge as K

log = logging.getLogger(__name__)

FACT_TAGS = ("description", "start_date", "architect", "artist_name", "artist", "historic", "heritage", "wikipedia",
             "wikidata", "height", "building:levels", "museum", "artwork_type", "memorial", "subject", "operator",
             "cuisine", "wheelchair", "fee", "opening_hours")
SUB_RU = {"museum": "музей", "gallery": "галерея", "theatre": "театр", "cinema": "кинотеатр", "park": "парк",
          "garden": "сад", "artwork": "арт-объект", "monument": "монумент", "memorial": "мемориал",
          "viewpoint": "смотровая площадка", "attraction": "достопримечательность", "cafe": "кафе",
          "restaurant": "ресторан", "mall": "торговый центр", "library": "библиотека", "fountain": "фонтан",
          "concert_hall": "концертный зал", "place_of_worship": "храм", "stadium": "стадион",
          "exhibition_centre": "выставочный центр"}
LANG_NAMES = {"ru": "русском", "kk": "казахском", "en": "английском"}
SYSTEM = ("Ты — голос городского гида Qydyr по Астане. Напиши короткий рассказ (60–90 слов) для человека, который "
          "стоит рядом с местом. Используй ТОЛЬКО факты из блока FACTS. Не придумывай даты, имена, легенды, цифры. "
          "Если фактов мало — подскажи, на что посмотреть и что сделать, без выдуманных подробностей. "
          "Пиши на {lang} языке, живо, без эмодзи, без списков.")
_CACHE: "OrderedDict[tuple[str, str], dict]" = OrderedDict()


def facts(row: dict, events: list[dict]) -> dict:
    attrs = row.get("attrs") or {}
    return {
        "name": row.get("name_ru") or row.get("name"),
        "kind": SUB_RU.get(row.get("subcategory") or "", row.get("subcategory") or row.get("category")),
        "tags": {k: attrs[k] for k in FACT_TAGS if attrs.get(k) not in (None, "")},
        "now": [f"{e['title']} — {describe_event(e)}" for e in events[:2]],
        "tip": K.IDEAS.get(row.get("subcategory") or "", ""),
    }


def plain(f: dict) -> str:
    parts = [f"{f['name']} — {f['kind']}."]
    t = f["tags"]
    if t.get("description"):
        parts.append(str(t["description"]).rstrip(".") + ".")
    if t.get("start_date"):
        parts.append(f"Год открытия по данным OpenStreetMap: {t['start_date']}.")
    if t.get("architect"):
        parts.append(f"Архитектор: {t['architect']}.")
    if t.get("artist_name") or t.get("artist"):
        parts.append(f"Автор: {t.get('artist_name') or t.get('artist')}.")
    if f["now"]:
        parts.append("Сейчас здесь: " + "; ".join(f["now"]) + ".")
    if f["tip"]:
        parts.append(f["tip"][0].upper() + f["tip"][1:] + ".")
    return " ".join(parts)


def tell(row: dict, events: list[dict], lang: str = "ru", llm=None) -> dict:
    key = (row["place_id"], lang)
    if key in _CACHE and not events:
        return _CACHE[key]
    f = facts(row, events)
    out = {"text": plain(f), "source": "facts", "facts": f}
    if llm is not None:
        try:
            text = llm.text(system=SYSTEM.format(lang=LANG_NAMES.get(lang, "русском")),
                            user="FACTS:\n" + json.dumps(f, ensure_ascii=False), max_tokens=400)
            if text:
                out = {"text": text.strip(), "source": "claude", "facts": f}
        except Exception as e:
            log.warning("story via Claude failed: %s", e)
    _CACHE[key] = out
    if len(_CACHE) > 1000:
        _CACHE.popitem(last=False)
    return out
