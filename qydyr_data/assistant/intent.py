"""Understanding the request.

Rules first (fast, offline, deterministic, ru/kk/en), then an optional LLM pass that can only *add*
understanding; it never overrides what the rules found with certainty.
Everything the system assumed (budget not given, walking by default) goes to `assumptions`,
which the UI shows as editable chips.
"""
from __future__ import annotations

import logging
import re
from datetime import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .models import ALL_NEEDS, Intent, Need, Overrides

log = logging.getLogger(__name__)

NEED_PATTERNS: dict[str, str] = {
    "coffee": r"кофе|кофейн|капучино|латте|раф\b|coffee|кофе ішу|кофе ішетін",
    "food": r"поесть|покушать|пообедать|поужинать|позавтракать|перекус|еда\b|ресторан|\bfood\b|\beat\b|тамақ",
    "socket": r"розетк|зарядит|подзаряд|socket|outlet|charge",
    "wifi": r"wi-?fi|вай-?фай|интернет",
    "quiet": r"тих|спокойн|без шума|поработать|позаниматься|почитать|quiet|calm|тыныш",
    "kids": r"с ребенк|с ребёнк|с детьми|с сыном|с дочк|с дочер|ребенк|ребёнк|детей|детск|балам|балалар|"
            r"\bkids?\b|child|children",
    "playground": r"площадк|playground|ойын алаң",
    "interactive": r"интерактив|interactive",
    "museum": r"музе|museum|мұражай",
    "culture": r"выставк|галере|театр|культур|искусств|exhibition|gallery|theatre|theater|көрме",
    "park": r"парк|сквер|природ|набережн|park|саябақ",
    "walk": r"прогуля|погулять|прогулк|пройтись|гулять|walk|серуен|қыдыр",
    "view": r"\bвид\b|видом|панорам|смотров|высот|достопримечат|что посмотреть|view|skyline|sightseeing|көрініс",
    "photo": r"фото|сфоткаться|фотосесс|инстаграм|instagram|\bинст\b|photo|достопримечат|что посмотреть|"
             r"посмотреть поблизости|sightseeing",
    "unusual": r"необычн|интересн|удиви|что-то новое|нестандартн|unusual|unique|weird|ерекше",
    "night": r"ночн|ночью|\bночи\b|поздно|после полуночи|\bnight\b|\blate\b|түнгі|түнде",
    "nightlife": r"\bбар\b|\bбары\b|\bбаре\b|клуб|вечеринк|туса|nightlife|party|\bbar\b",
    "free": r"бесплатн|без денег|(?<!step-)(?<!step )\bfree\b|тегін",
    "indoor": r"в помещени|внутри|под крышей|погреться|согреться|не мерзнуть|не мёрзнуть|indoor|жылы жер",
    "accessible": r"коляск|инвалидн|без ступен|пандус|маломобил|мүгедек|wheelchair|step-?free|accessible",
    "romantic": r"свидани|романтич|романтик|с девушкой|с парнем|\bdate\b|romantic",
    "shopping": r"шопинг|магазин|торгов|shopping",
    "event": r"событи|мероприят|концерт|афиш|спектакл|\bevent|іс-шара|стендап|стенд-ап|stand-?up|квиз|quiz|"
             r"\bшоу\b|фестивал|festival|лекци|мастер-класс|мастер класс|workshop|кинопоказ|киноужин|"
             r"мюзикл|\bопер[аеуы]\b|балет|\bgig\b|\bshow\b|что ид[её]т|что сегодня ид[её]т|what'?s on|не болып жатыр",
}
_NEED_RE = {k: re.compile(v, re.I) for k, v in NEED_PATTERNS.items()}
# "хочу на концерт" is not "any event": the kind narrows what counts (a kids' show is not a concert)
EVENT_KIND_PATTERNS: dict[str, str] = {
    "concert": r"концерт|\bgig\b|concert", "standup": r"стендап|стенд-ап|stand-?up|комик",
    "exhibition": r"выставк|exhibition|көрме", "performance": r"спектакл|мюзикл|\bопер[аеуы]\b|балет|performance",
    "film": r"\bкино\b|фильм|кинопоказ|киноужин|\bmovie", "lecture": r"лекци|lecture",
    "workshop": r"мастер-класс|мастер класс|workshop", "festival": r"фестивал|festival",
    "party": r"вечеринк|\bparty\b", "excursion": r"экскурси|excursion",
}
_KIND_RE = {k: re.compile(v, re.I) for k, v in EVENT_KIND_PATTERNS.items()}


def event_kind_fits(e: dict, kinds: list[str]) -> bool:
    """The listing's kind is a guess too: "Концерт «Хиты на все времена»" filed as "other" is still a concert."""
    return not kinds or e.get("kind") in kinds or any(_KIND_RE[k].search(e.get("title") or "") for k in kinds)

ANCHOR_PREPS = r"(?:возле|около|рядом с|у|в районе|недалеко от|напротив|в|на|из|near|at|in|by|from)"
HERE_PREPS = {"возле", "около", "рядом с", "у", "в районе", "недалеко от", "напротив", "near", "at", "by"}
# "хочу в Хан Шатыр" is where to go, "кофе в Хан Шатыре" is where the person is
MOTION_BEFORE = re.compile(r"(?:хочу|хотим|хочется|пойти|пойду|пойд[её]м|сходить|схожу|сходим|поехать|поеду|"
                           r"поедем|ехать|еду|едем|идти|иду|ид[её]м|попасть|go|going|get|want|to)\s*$", re.I)
KK_POSTPOSITIONS = re.compile(r"([\w-]+(?:\s+[\w-]+){0,3})\s+(?:жанында|қасында|маңында|алдында|ішінде|"
                              r"жанына|қасына)\w*", re.I)
STOP_WORDS = {"меня", "нас", "тебя", "вас", "него", "неё", "нее", "них", "ребенка", "ребёнка", "детьми",
              "ребенком", "ребёнком", "час", "часа", "минут", "наличии", "запасе", "городе", "астане",
              "astana", "astane", "течение", "итоге", "общем", "целом", "свободное", "свободном", "my", "the"}


def detect_lang(text: str) -> str:
    if re.search(r"[әғқңөұүһі]", text, re.I):
        return "kk"
    cyr = len(re.findall(r"[а-яё]", text, re.I))
    lat = len(re.findall(r"[a-z]", text, re.I))
    return "en" if lat > cyr else "ru"


def _num(s: str) -> float:
    return float(s.replace(",", "."))


NUM_WORDS = {"один": 1, "одного": 1, "полтора": 1.5, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
             "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "бір": 1, "екі": 2, "үш": 3, "төрт": 4, "бес": 5}
_NUM = "|".join(NUM_WORDS)
_CLOCK_BEFORE = re.compile(r"(?:\bв|\bк|\bдо|\bс|\bпосле|\bat|\buntil|\bby|\bсағат)\s*$")


def _hours(t: str, pattern: str) -> re.Match | None:
    """First "N часа" that is an amount of time, not a clock time ("в 2 часа ночи")."""
    return next((m for m in re.finditer(pattern, t) if not _CLOCK_BEFORE.search(t[:m.start()])), None)


def parse_budget(text: str) -> tuple[int | None, int | None]:
    t = text.lower()
    m = re.search(r"(\d+(?:[.,]\d)?)\s*[–—-]\s*(\d+(?:[.,]\d)?)\s*(час|ч\b|hour|сағат)", t)
    if m:
        return int(_num(m[1]) * 60), int(_num(m[2]) * 60)
    m = re.search(r"(\d+)\s*[–—-]\s*(\d+)\s*(мин|min|минут)", t)
    if m:
        return int(m[1]), int(m[2])
    m = _hours(t, r"(\d+(?:[.,]\d)?)\s*(час|ч\b|hour|сағат)")
    if m:
        v = int(_num(m[1]) * 60)
        return v, v
    # "два часа", "two hours", "екі сағат", and the casual "часа два" (about two hours)
    m = _hours(t, rf"\b({_NUM})\s+(?:час|hour|сағат)") or re.search(rf"\bчас(?:а|ов)\s+({_NUM}|\d)\b(?!\s*(?:ноч|утр|дня|вечер))", t)
    if m:
        v = round((NUM_WORDS.get(m[1]) or _num(m[1])) * 60)
        return v, v
    m = re.search(r"(\d+)\s*(мин|min|минут)", t) or re.search(r"(?:минут|минуты)\s+(\d+)", t)  # "минут 40"
    if m:
        return int(m[1]), int(m[1])
    if re.search(r"полчаса|пол часа|half an hour|жарты сағат", t):
        return 30, 30
    if re.search(r"полтора час", t):
        return 90, 90
    if re.search(r"пар[уыа] час|couple of hours|екі-үш сағат", t):
        return 120, 120
    if re.search(r"\bчас\b|\bчасик|an hour|бір сағат", t):
        return 60, 60
    return None, None


PART_OF_DAY = [  # (pattern, start time)
    (r"утром|с утра|\bmorning\b|таңертең", time(10, 0)),
    (r"днём|днем|в обед|\bafternoon\b|түсте", time(14, 0)),
    (r"вечером|вечерком|\bevening\b|tonight|кешке|кеште", time(19, 0)),
]


WEEKDAYS = [  # (pattern, weekday), Monday = 0
    (r"понедельник|monday|дүйсенбі", 0), (r"вторник|tuesday|сейсенбі", 1), (r"сред[ау]|wednesday|сәрсенбі", 2),
    (r"четверг|thursday|бейсенбі", 3), (r"пятниц|friday|жұма", 4), (r"суббот|saturday|сенбі", 5),
    (r"воскресень|sunday|жексенбі", 6),
]


def parse_weekday(low: str) -> int | None:
    """"в субботу", "on Friday". Weekend means Saturday."""
    for pat, wd in WEEKDAYS:
        if re.search(pat, low):
            return wd
    return 5 if re.search(r"выходн|weekend|демалыс", low) else None


_NOT_HOUR = r"(?![:.,]\d|\s*(?:мин|min|человек|чел|раз|лет|год|км|м\b|%|тыс|тг|тенге))"


def _clock_hour(h: int, part: str | None = None) -> int:
    """"7 вечера" -> 19, "2 ночи" -> 2; a bare "в 7" is 19:00: nobody plans a walk at 7 am without saying so."""
    if part in ("вечера", "дня"):
        return h % 12 + 12
    if part in ("утра", "ночи"):
        return h % 12
    return h + 12 if 1 <= h <= 7 else h % 24


def parse_start(low: str) -> tuple[time | None, int]:
    """When the person plans to go: "в 19:30", "в 7 вечера", "вечером", "завтра". Not "до 19:00" (a deadline)."""
    days = 1 if re.search(r"завтра|tomorrow|ертең", low) else 0
    if m := re.search(r"(?:\bв|\bк|\bна|\bat|\bсағат)\s+(\d{1,2})[:.](\d{2})\b", low):
        return time(int(m[1]) % 24, int(m[2])), days
    if m := re.search(r"\b(?:в|к)\s+(\d{1,2})\s*(?:час\w*\s+)?(вечера|дня|утра|ночи)", low):
        return time(_clock_hour(int(m[1]), m[2]), 0), days
    if m := re.search(rf"(?:\bв|\bк|\bat)\s+(\d{{1,2}})(?:\s*час(?:а|ов)?)?\b{_NOT_HOUR}", low):  # "сегодня в 20"
        if int(m[1]) <= 23:
            return time(_clock_hour(int(m[1])), 0), days
    for pat, t in PART_OF_DAY:
        if re.search(pat, low):
            return t, days
    return (time(12, 0) if days else None), days


def parse_rules(text: str) -> Intent:
    t = text.strip()
    low = t.lower()
    it = Intent(text=t, lang=detect_lang(t))

    # in the order the person said them: the first one is what they want most
    found = [(m.start(), n) for n in ALL_NEEDS if (m := _NEED_RE[n].search(low))]
    it.needs = [n for _, n in sorted(found)]
    # Kazakh "бар ма?" means "is there?", not a bar
    if it.lang == "kk" and "nightlife" in it.needs and not re.search(r"клуб|паб|pub|түнгі клуб|сыра", low):
        it.needs.remove("nightlife")
    it.with_kids = "kids" in it.needs
    if re.search(r"коляск|инвалидн|маломобил|без ступен|пандус|мүгедек|wheelchair|step-?free", low):
        it.wheelchair = True
        if "accessible" not in it.needs:
            it.needs.append("accessible")

    it.budget_min, it.budget_max = parse_budget(low)

    if re.search(r"вылет|самол[её]т|рейс|аэропорт|flight|airport|ұшақ|әуежай", low):
        it.deadline = "flight"
    elif m := re.search(r"\bдо\s+(\d{1,2})[:.](\d{2})|\buntil\s+(\d{1,2})[:.](\d{2})", low):
        hh, mm = (m[1], m[2]) if m[1] else (m[3], m[4])
        it.deadline, it.deadline_time = "time", time(int(hh) % 24, int(mm))
    elif m := re.search(rf"\b(?:до|until)\s+(\d{{1,2}})(?:\s*час\w*)?(?:\s+(вечера|дня|утра|ночи))?\b{_NOT_HOUR}", low):
        if int(m[1]) <= 23:  # "до 8 вечера", "до 18"
            it.deadline, it.deadline_time = "time", time(_clock_hour(int(m[1]), m[2]), 0)

    it.start_time, it.start_days = parse_start(low)
    it.start_weekday = parse_weekday(low)
    if it.start_weekday is not None and it.start_time is None:
        it.start_time = time(12, 0)

    if re.search(r"на такси|на машине|на авто|by taxi|by car|таксимен", low):
        it.mode = "taxi"
    if re.search(r"турист|приезж|впервые в астан|гость столицы|tourist|first time|visiting", low):
        it.persona = "tourist"
    elif it.with_kids:
        it.persona = "family"
    if m := re.search(r"нас\s+(\d+)|(\d+)\s+человек|(\d+)\s+(?:people|of us)", low):
        it.group_size = int(next(g for g in m.groups() if g))
    elif re.search(r"с друзьями|компани|группой|with friends", low):
        it.group_size = 3

    it.want_route = bool(re.search(r"маршрут|route|по пути", low)) or (it.budget_max or 0) >= 90
    it.event_kinds = [k for k, rx in _KIND_RE.items() if rx.search(low)]
    cands = anchor_candidates(t)
    it.anchor_text = next((p for p, here in cands if here), cands[0][0] if cands else None)
    return it


def anchor_candidates(text: str) -> list[tuple[str, bool]]:
    """Every phrase that may name where the person is, in order: 'хочу на концерт, я у Байтерека' ->
    [('концерт', False), ('Байтерека', True)]. The flag says it reads as "I am here" (у, возле, жанында,
    "кофе в Хан Шатыре"), not as a destination ("хочу в Хан Шатыр") — only those beat a GPS point."""
    out: list[tuple[int, str, bool]] = []
    for m in re.finditer(rf"(?:^|(?<=[\s,(])){ANCHOR_PREPS}\s+(?=[«\"]?\w)", text, re.I):
        prep = " ".join(m[0].lower().split())
        words = re.split(r"[,.!?;:—–()]", text[m.end():], maxsplit=1)[0].strip(" «»\"").split()
        if not words or words[0].lower() in STOP_WORDS or words[0].isdigit() or words[0].lower() in NUM_WORDS:
            continue
        here = prep in HERE_PREPS or (prep in ("в", "на", "in") and not MOTION_BEFORE.search(text[:m.start()]))
        out.append((m.start(), " ".join(words[:5]), here))
    out += [(m.start(), m[1], True) for m in KK_POSTPOSITIONS.finditer(text)]  # "Бәйтерек жанындамын"
    return [(p, here) for _, p, here in sorted(out)]


ASSUME_TIME = "около часа (время не указано)"
ASSUME_FLIGHT = "до вылета ~2 часа (уточните)"
ASSUME_WALK = "пешком"
ASSUME_STROLL = "просто погулять: прогулка, культура, кофе"


def apply_defaults(it: Intent, explicit: set[str] | frozenset[str] = frozenset()) -> Intent:
    """Fill what the person did not say, and say so (chips). `explicit` = fields set by chip taps."""
    if it.budget_min is None and it.budget_max is None:
        if it.deadline == "flight":
            it.budget_min = it.budget_max = 120
            it.assumptions.append(ASSUME_FLIGHT)
        else:
            it.budget_min, it.budget_max = 45, 75
            it.assumptions.append(ASSUME_TIME)
    if it.mode == "walk" and "mode" not in explicit and not re.search(r"пешком|on foot|жаяу", it.text, re.I):
        it.assumptions.append(ASSUME_WALK)
    if not it.needs:
        if it.only_events:
            it.needs = ["event"]
        else:
            it.needs = ["walk", "culture", "coffee"]
            it.assumptions.append(ASSUME_STROLL)
    return it


def apply_overrides(it: Intent, ov: Overrides | None) -> set[str]:
    """Chip taps on top of the parsed request. Returns the names of fields the person set explicitly."""
    if ov is None:
        return set()
    explicit: set[str] = set()
    if ov.budget_max is not None:
        it.budget_max = ov.budget_max
        it.budget_min = ov.budget_min if ov.budget_min is not None else ov.budget_max
        explicit.add("budget")
    if ov.mode is not None:
        it.mode = ov.mode
        explicit.add("mode")
    if ov.with_kids is not None:
        it.with_kids = ov.with_kids
        if ov.with_kids and "kids" not in it.needs:
            it.needs.append("kids")
        if not ov.with_kids:
            it.needs = [n for n in it.needs if n != "kids"]
            if it.persona == "family":
                it.persona = None
        explicit.add("kids")
    if ov.wheelchair is not None:
        it.wheelchair = ov.wheelchair
        if ov.wheelchair and "accessible" not in it.needs:
            it.needs.append("accessible")
        if not ov.wheelchair:
            it.needs = [n for n in it.needs if n != "accessible"]
        explicit.add("wheelchair")
    if ov.add_needs or ov.remove_needs:
        it.needs = [n for n in list(dict.fromkeys(list(it.needs) + list(ov.add_needs))) if n not in ov.remove_needs]
        explicit.add("needs")
    if ov.only_events is not None:
        it.only_events = ov.only_events
        explicit.add("only_events")
    if ov.lang is not None:
        it.lang = ov.lang
    return explicit


class LLMIntent(BaseModel):
    """What Claude fills. Every field required, no nullable unions: 0 / "" / false mean "not said"."""
    model_config = ConfigDict(extra="forbid")
    anchor_text: str = Field(description='the place the person is at now, as they named it, or ""')
    budget_min: int = Field(description="free time lower bound in minutes, 0 if not said")
    budget_max: int = Field(description="free time upper bound in minutes, 0 if not said")
    needs: list[Need]
    with_kids: bool
    wheelchair: bool
    persona: Literal["", "local", "tourist", "student", "family"]
    deadline_flight: bool
    group_size: int = Field(description="1 if not said")


class ClaudeIntentParser:
    """LLM pass for requests the rules can't fully read ("хочу куда-нибудь, где можно согреться
    и не тратить деньги, ребёнку 4"). Its output is merged on top of the rules, never replaces them."""

    SYSTEM = (
        "Разбери запрос жителя или туриста Астаны о том, куда сходить, в структуру по схеме. "
        "needs — только то, что человек хочет (из списка в схеме), самое важное первым. "
        "Ничего не придумывай: не сказано — 0, пустая строка, false или пустой список."
    )

    def __init__(self, llm=None) -> None:
        if llm is None:
            from .. import settings
            from ..llm import Claude
            llm = Claude(settings.CLAUDE_MODEL, fast=True)
        self.llm = llm

    def parse(self, text: str) -> Intent:
        got = self.llm.structured(system=self.SYSTEM, user=text, schema=LLMIntent, max_tokens=2048)
        it = parse_rules(text)
        it.anchor_text = it.anchor_text or (got.anchor_text.strip() or None)
        if it.budget_max is None and got.budget_max > 0:
            it.budget_min, it.budget_max = (got.budget_min or got.budget_max), got.budget_max
        it.needs = list(dict.fromkeys(list(it.needs) + list(got.needs)))  # type: ignore[arg-type]
        it.with_kids = it.with_kids or got.with_kids
        it.wheelchair = it.wheelchair or got.wheelchair
        if it.with_kids and "kids" not in it.needs:
            it.needs.append("kids")
        if got.deadline_flight and not it.deadline:
            it.deadline = "flight"
        if got.persona and not it.persona:
            it.persona = got.persona  # type: ignore[assignment]
        if got.group_size > 1 and it.group_size == 1:
            it.group_size = got.group_size
        return it


def understand(text: str, *, lat: float | None = None, lon: float | None = None,
               llm: ClaudeIntentParser | None = None, defaults: bool = True) -> Intent:
    try:
        it = llm.parse(text) if llm else parse_rules(text)
    except Exception as e:  # the LLM is a bonus, never a single point of failure
        log.warning("LLM intent parsing failed (%s); using rules", e)
        it = parse_rules(text)
    if lat is not None and lon is not None:
        it.lat, it.lon = lat, lon
    return apply_defaults(it) if defaults else it
