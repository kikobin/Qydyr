"""Group mode: 3–5 people, each with their own wish. Strategy "nobody suffers" (least misery):
the group score of a place is mostly the score of the least happy member."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from . import knowledge as K
from .intent import parse_rules
from .models import FitMark, Intent, MemberFit

MARK = {"yes": "да", "likely": "вероятно", "unknown": "неизвестно", "no": "нет"}


@dataclass
class Member:
    name: str
    intent: Intent


def parse_members(lines: list[str]) -> list[Member]:
    """["Айгерим: кофе и тихо", "Данияр: хочу на смотровую"]"""
    out = []
    for i, line in enumerate(lines):
        name, sep, wish = line.partition(":")
        if not sep:
            name, wish = f"Участник {i + 1}", line
        out.append(Member(name=name.strip(), intent=parse_rules(wish.strip())))
    return out


def combine(base: Intent, members: list[Member]) -> Intent:
    counts: dict[str, int] = {}
    for m in members:
        for n in m.intent.needs:
            counts[n] = counts.get(n, 0) + 1
    merged = sorted(counts, key=lambda n: -counts[n])
    base.needs = list(dict.fromkeys(merged + list(base.needs)))  # type: ignore[arg-type]
    base.with_kids = base.with_kids or any(m.intent.with_kids for m in members)
    base.wheelchair = base.wheelchair or any(m.intent.wheelchair for m in members)
    maxes = [m.intent.budget_max for m in members if m.intent.budget_max]
    if maxes:
        base.budget_max = min(maxes + ([base.budget_max] if base.budget_max else []))
        base.budget_min = min(base.budget_min or base.budget_max, base.budget_max)
    base.group_size = max(base.group_size, len(members))
    return base


def member_score(c: K.Cand, intent: Intent, at: datetime) -> float:
    needs = intent.needs
    if not needs:
        return 0.5
    total = wsum = 0.0
    for i, n in enumerate(needs):
        w = 1.6 if i == 0 else 1.0
        total += w * K.WEIGHT[K.need_match(c, n, at=at)[0]]
        wsum += w
    return total / wsum


def group_detail(c: K.Cand, members: list[Member], at: datetime) -> list[MemberFit]:
    return [MemberFit(name=m.name, marks=[FitMark(need=n, label=K.NEED_LABELS[n],
                                                  certainty=K.need_match(c, n, at=at)[0])  # type: ignore[arg-type]
                                          for n in m.intent.needs[:2]])
            for m in members]


def group_score(c: K.Cand, members: list[Member], at: datetime) -> tuple[float, list[str]]:
    scores = [member_score(c, m.intent, at) for m in members]
    expl = []
    for mf in group_detail(c, members, at):
        marks = [f"{f.label} — {MARK[f.certainty]}" for f in mf.marks]
        expl.append(f"{mf.name}: {', '.join(marks) if marks else 'без пожеланий'}")
    return 0.7 * min(scores) + 0.3 * (sum(scores) / len(scores)), expl


def meeting_point(points: list[tuple[float, float]], cands: list[tuple[str, float, float]]) -> tuple[str, int] | None:
    """Fair meeting place for people coming from different spots: the candidate that minimises the
    longest walk (nobody walks much more than the others). Returns (candidate id, max walk minutes)."""
    from . import travel
    best = None
    for cid, lat, lon in cands:
        worst = max(travel.walk_min(p[0], p[1], lat, lon) for p in points)
        if best is None or worst < best[1]:
            best = (cid, worst)
    return best
