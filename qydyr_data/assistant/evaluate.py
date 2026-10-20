"""Scenario evaluation: run requests through the assistant and check every answer against rules
that must always hold (fits the time, not closed on arrival, has a "why", honest labels...) plus
per-scenario expectations. This is how we prove quality with numbers instead of words."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from .. import settings
from . import knowledge as K
from .models import Answer, Overrides
from .recommend import Assistant, DbData
from .render import render

EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B50\uFE0F]")


@dataclass
class Result:
    id: str
    ok: bool
    summary: str
    failures: list[str] = field(default_factory=list)
    answer: Answer | None = None


def invariants(ans: Answer) -> list[str]:
    """Rules every answer must satisfy."""
    f: list[str] = []
    it = ans.intent
    if len(ans.items) > 3:
        f.append(f"больше 3 пунктов ({len(ans.items)})")
    budget = it.budget_max or 75
    for i in ans.items:
        tag = f"«{i.title}»"
        if not i.why:
            f.append(f"{tag}: нет объяснения «почему»")
        if not i.what_to_do:
            f.append(f"{tag}: нет «что делать»")
        if not i.sources:
            f.append(f"{tag}: нет источника")
        if i.visit_min[0] <= 0 or i.visit_min[0] > i.visit_min[1]:
            f.append(f"{tag}: странная длительность {i.visit_min}")
        if i.arrive_at + timedelta(minutes=i.visit_min[0]) > ans.at + timedelta(minutes=budget + 1):
            f.append(f"{tag}: не укладывается в {budget} мин")
        if i.status.startswith("откроется") and i.arrive_at < ans.at:
            f.append(f"{tag}: закрыто на момент прихода")
        if not 0 <= i.confidence <= 1:
            f.append(f"{tag}: уверенность вне [0,1]")
        if it.with_kids and i.subcategory in K.ADULT_ONLY:
            f.append(f"{tag}: не для детей")
        if "открыто сейчас" in i.status.lower() and i.status == "часы работы неизвестны":
            f.append(f"{tag}: уверенное «открыто» без данных")
    if ans.route and ans.route.total_min > budget + 1:
        f.append(f"маршрут {ans.route.total_min} мин длиннее бюджета {budget}")
    if ans.anchor and ans.anchor.source == "gps" and not any("не сохраняется" in u for u in ans.used_data):
        f.append("нет раскрытия про геолокацию")
    if ans.anchor and not ans.used_data:
        f.append("не указано, какие данные использованы")
    steps = [x.n for x in ans.funnel]
    if steps and (steps != sorted(steps, reverse=True) or steps[-1] != len(ans.items)):
        f.append(f"воронка не сходится: {steps}")
    if any(not r.reason for r in ans.rejected):
        f.append("у отброшенного места нет причины")
    if ans.context.simulated and not any(n.startswith("Симуляция") for n in ans.notes):
        f.append("симуляция погоды не подписана")
    text = render(ans)
    if EMOJI.search(text):
        f.append("в ответе есть эмодзи")
    return f


def expectations(ans: Answer, exp: dict) -> list[str]:
    f: list[str] = []
    items = ans.items
    titles = [i.title for i in items]
    if (n := exp.get("min_items")) is not None and len(items) < n:
        f.append(f"ожидалось ≥{n} пунктов, есть {len(items)}")
    if (n := exp.get("max_items")) is not None and len(items) > n:
        f.append(f"ожидалось ≤{n} пунктов, есть {len(items)}")
    if (s := exp.get("anchor_contains")) and (not ans.anchor or s.lower() not in ans.anchor.label.lower()):
        f.append(f"место не распознано как «{s}»")
    if (s := exp.get("first_title_contains")) and (not items or s.lower() not in items[0].title.lower()):
        f.append(f"первым ожидался «{s}», а не {titles[:1]}")
    if (s := exp.get("any_title_contains")) and not any(s.lower() in t.lower() for t in titles):
        f.append(f"нет «{s}» среди {titles}")
    if (s := exp.get("first_status_contains")) and (not items or s.lower() not in items[0].status.lower()):
        f.append(f"статус первого не содержит «{s}»: {items[0].status if items else '-'}")
    for s in exp.get("no_subcategories", []):
        if any(i.subcategory == s for i in items):
            f.append(f"не должно быть {s}")
    if exp.get("no_outdoor_items") and any(i.indoor == "outdoor" for i in items):
        f.append("в мороз предложено место под открытым небом")
    if (need := exp.get("first_need_yes")) and (not items or items[0].need_hits.get(need) != "yes"):
        f.append(f"у первого пункта нет «{need}»")
    if (s := exp.get("note_contains")) and not any(s.lower() in n.lower() for n in ans.notes):
        f.append(f"нет пояснения «{s}»")
    if (lang := exp.get("lang")) and ans.intent.lang != lang:
        f.append(f"язык {ans.intent.lang}, ожидался {lang}")
    for n in exp.get("needs_include", []):
        if n not in ans.intent.needs:
            f.append(f"не понял потребность «{n}»")
    if exp.get("group_fit") and not all(i.group_fit for i in items):
        f.append("нет объяснения для каждого участника")
    if exp.get("route") and not ans.route:
        f.append("ожидался мини-маршрут")
    if exp.get("only_events") and not all(i.kind == "event" or i.attached_event for i in items):
        f.append("в режиме «только события» есть место без события")
    if (code := exp.get("rejected_reason")) and not any(r.reason_code == code for r in ans.rejected):
        f.append(f"нет отброшенного по причине «{code}»")
    if (s := exp.get("leave_by_first")) and (not items or not items[0].leave_by):
        f.append("у первого пункта нет «выходите до»")
    return f


def run_file(conn, path: Path) -> list[Result]:
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    assistant = Assistant(DbData(conn))
    out = []
    for sc in spec["scenarios"]:
        at = datetime.fromisoformat(sc["at"]).replace(tzinfo=settings.TZ) if sc.get("at") else None
        wf = settings.ROOT / sc["weather"] if sc.get("weather") else None
        af = settings.ROOT / sc["air"] if sc.get("air") else None
        ov = Overrides.model_validate(sc["overrides"]) if sc.get("overrides") else None
        ans = assistant.ask(sc["text"], at=at, lat=sc.get("lat"), lon=sc.get("lon"), weather_file=wf, air_file=af,
                            fetch_weather=wf is None and not sc.get("no_weather"), members=sc.get("members"),
                            weather_preset=sc.get("preset"), overrides=ov, surprise=bool(sc.get("surprise")),
                            seed=sc.get("seed"))
        fails = invariants(ans) + expectations(ans, sc.get("expect", {}))
        summary = " | ".join(i.title for i in ans.items) or (ans.notes[-1] if ans.notes else "—")
        out.append(Result(id=sc["id"], ok=not fails, summary=summary[:110], failures=fails, answer=ans))
    return out
