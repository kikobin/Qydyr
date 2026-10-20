"""Pick 1–3 things (and a mini-route) that actually fit.

Hard constraints first — you get there in time, it is open when you arrive and long enough to be
worth it, the weather allows it, it suits a child / a wheelchair if needed. Then scoring by how well
it matches the request, what is on there right now, distance and weather. The "why" is built only
from facts the system has (data, hours, events, weather), never from the model's imagination.

Every dropped candidate keeps its reason, so the answer can show "почему не это" and the decision
funnel: nearby -> reachable -> open on arrival -> fits the request -> picked.
"""
from __future__ import annotations

import math
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from .. import settings, store
from ..badges import MONTHS_GEN, describe_event
from ..events.venues import PlaceLite
from . import air as A
from . import knowledge as K
from . import travel
from . import weather as W
from .group import Member, combine, group_detail, group_score, parse_members
from .intent import (ASSUME_FLIGHT, ASSUME_STROLL, ASSUME_TIME, ASSUME_WALK, ClaudeIntentParser, apply_defaults,
                     apply_overrides, event_kind_fits, understand)
from .locate import Locator, key_set
from .models import (AirInfo, Anchor, Answer, Chip, ChipOption, Comfort, Context, FunnelStep, Intent, Item,
                     Overrides, Rejected, WeatherInfo)
from .privacy import coarse
from .route import build_route

AIRPORT_BUFFER_MIN = 60   # domestic flight; shown to the person as an assumption they can change
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
BUDGETS = [("30 мин", 20, 30), ("1 час", 45, 60), ("1,5 часа", 75, 90), ("2 часа", 100, 120), ("3 часа", 150, 180)]
SURPRISE_NEEDS = ["unusual", "event", "photo"]
STAGE = {"far": "reach", "flight": "reach", "cold_walk": "reach", "closed": "open", "short": "open", "wait": "open",
         "weather": "fit", "kids": "fit", "wheelchair": "fit", "fit": "fit", "not_event": "fit"}


class DataSource(Protocol):
    def places_near(self, lat: float, lon: float, radius_m: float, at: datetime, limit: int) -> list[dict]: ...
    def whats_on(self, lat: float, lon: float, radius_m: float, at: datetime, horizon_min: int) -> list[dict]: ...
    def places_lite(self) -> list[PlaceLite]: ...


class DbData:
    def __init__(self, conn) -> None:
        self.conn = conn

    def places_near(self, lat, lon, radius_m, at, limit):
        return store.places_near(self.conn, lat, lon, radius_m, at, None, limit)

    def whats_on(self, lat, lon, radius_m, at, horizon_min):
        return store.whats_on(self.conn, lat, lon, radius_m, at, horizon_min)

    def places_lite(self):
        return store.load_places_lite(self.conn)


@dataclass
class Scored:
    cand: K.Cand
    walk: int
    arrive: datetime
    start_at: datetime | None
    window_end: datetime | None
    visit: tuple[int, int]
    score: float
    need_score: float
    hits: dict[str, tuple[str, str]]
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    confidence: float = 0.8
    group_expl: list[str] = field(default_factory=list)
    distance_m: int = 0
    closes_first: bool = False      # window ends because the place closes / the event ends
    taxi: bool = False              # `walk` is a taxi ride (taxi mode and too far to walk)


@dataclass
class Reject:
    cand: K.Cand
    code: str
    reason: str
    walk: int
    distance_m: int


def _mins(td: timedelta) -> int:
    return int(td.total_seconds() // 60)


def _hm(dt: datetime) -> str:
    return dt.astimezone(settings.TZ).strftime("%H:%M")


def _when(dt: datetime, at: datetime) -> str:
    d, today = dt.astimezone(settings.TZ).date(), at.astimezone(settings.TZ).date()
    if d == today:
        return f"в {_hm(dt)}"
    if d == today + timedelta(days=1):
        return f"завтра в {_hm(dt)}"
    return f"{d.day} {MONTHS_GEN[d.month - 1]} в {_hm(dt)}"


def _h(m: int) -> str:
    return f"{m // 60}" if m % 60 == 0 else f"{m / 60:.1f}".replace(".", ",")


def fmt_budget(lo: int, hi: int) -> str:
    if lo == hi:
        return f"{hi} мин" if hi < 60 or hi % 30 else f"{_h(hi)} ч"
    if lo >= 60 and lo % 30 == 0 and hi % 30 == 0:
        return f"{_h(lo)}–{_h(hi)} ч"
    if lo >= hi * 0.65:  # a chip option like "2 часа" = 100–120 min reads as "до 2 ч"
        return f"до {_h(hi)} ч" if hi >= 60 and hi % 30 == 0 else f"до {hi} мин"
    return f"{lo}–{hi} мин"


def display_name(row: dict, lang: str) -> str:
    if lang == "ru" and row.get("name_ru"):
        return row["name_ru"]
    if lang == "kk" and row.get("name_kk"):
        return row["name_kk"]
    if lang == "en" and row.get("name_en"):
        return row["name_en"]
    return row["name"]


def cand_from_place(r: dict, lang: str = "ru") -> K.Cand:
    attrs = r.get("attrs") or {}
    c = K.Cand(kind="place", id=r["place_id"], title=display_name(r, lang), lat=r["lat"], lon=r["lon"],
               category=r["category"], subcategory=r["subcategory"], indoor=r["indoor"], attrs=attrs,
               description=attrs.get("description"), open_state=r["open_state"], closes_at=r["closes_at"],
               opens_at=r["opens_at"], source_url=r.get("source_url"),
               confidence=0.85 if r["open_state"] != "unknown" else 0.7)
    if K.indoor_attraction(c):  # a roller coaster on the 4th floor of a mall is not a landmark outside
        c.indoor, c.category = "indoor", "entertainment"
    return c


def relevance(c: K.Cand, needs: list[str]) -> float:
    """How much a candidate is 'what was asked', ignoring time and weather (for 'почему не это')."""
    if not needs:
        return 0.5
    base = sum(K.WEIGHT[K.need_match(c, n)[0]] for n in needs) / len(needs)
    return base + (0.15 if c.category in ("landmark", "culture") else 0.0) + (0.2 if c.attached else 0.0)


class Assistant:
    def __init__(self, data: DataSource, llm: ClaudeIntentParser | None = None,
                 places: list[PlaceLite] | None = None, narrator=None) -> None:
        self.data = data
        self.llm = llm
        self.narrator = narrator    # RAG generation: Claude words the picked places' facts (assistant/narrate.py)
        self.picked: list[Scored] = []  # the last answer's picks with their facts, for a narrator called later
        self.locator = Locator.of(places) if places is not None else Locator(data.places_lite())

    # ------------------------------------------------------------------ public
    def ask(self, text: str, *, at: datetime | None = None, lat: float | None = None, lon: float | None = None,
            weather: W.Weather | None = None, weather_file=None, fetch_weather: bool = True,
            members: list[str] | None = None, overrides: Overrides | None = None,
            weather_preset: str | None = None, air: A.Air | None = None, air_file=None,
            surprise: bool = False, seed: int | None = None, anchor: Anchor | None = None) -> Answer:
        at = (at or datetime.now(settings.TZ)).astimezone(settings.TZ)
        group = parse_members(members) if members else []
        intent = understand(text, lat=lat, lon=lon, llm=self.llm, defaults=False)
        if intent.start_time:  # "вечером", "завтра в 11:00", "в субботу": answer for that moment, if it is still ahead
            days = intent.start_days
            if intent.start_weekday is not None:
                days = (intent.start_weekday - at.weekday()) % 7
            planned = datetime.combine(at.date() + timedelta(days=days), intent.start_time, tzinfo=at.tzinfo)
            if planned <= at and not days and intent.start_time.hour < 6:
                planned += timedelta(days=1)  # "в 2 ночи" said in the afternoon is the coming night
            if planned > at:
                at = planned
        if group:
            intent = combine(intent, group)
        explicit = apply_overrides(intent, overrides)
        if intent.deadline == "time" and intent.deadline_time and intent.budget_max is None:
            # "свободен до 18:00": the free time is the time until then, not a guessed hour
            dl = datetime.combine(at.date(), intent.deadline_time, tzinfo=settings.TZ)
            if dl <= at:
                dl += timedelta(days=1)
            if 10 <= _mins(dl - at) <= 12 * 60:
                intent.budget_min = intent.budget_max = _mins(dl - at)
        if surprise:
            intent.needs = list(dict.fromkeys(SURPRISE_NEEDS + list(intent.needs)))  # type: ignore[arg-type]
        intent = apply_defaults(intent, explicit)
        anchor = anchor or self.locator.resolve(intent)
        buffer = (overrides.airport_buffer_min if overrides and overrides.airport_buffer_min else AIRPORT_BUFFER_MIN)
        ans = Answer(id=uuid.uuid4().hex[:10], intent=intent, anchor=anchor, at=at, surprise=surprise)
        ans.chips = self._chips(intent, anchor, buffer)
        if anchor is None:
            ans.notes.append("Поделитесь геолокацией или назовите место рядом — например, «я у Mega Silk Way».")
            return ans

        # --- weather, air, what-if ---
        live = False
        if weather is None and (weather_file or fetch_weather):
            weather = W.get(anchor.lat, anchor.lon, at, weather_file)
            live = weather is not None and not weather_file
        if air is None and (air_file or live):
            air = A.get(anchor.lat, anchor.lon, at, air_file)
        if weather is not None and air is not None:
            weather.air = air
        if weather_preset:
            weather = W.preset(weather_preset, at, real=weather, lat=anchor.lat, lon=anchor.lon)
            ans.notes.append(f"Симуляция погоды: «{weather.simulated}». Места, часы работы и события настоящие.")
        if weather:
            ans.weather = weather.summary()
            ans.notes += [w[0].upper() + w[1:] for w in weather.warnings(at)]

        # --- the time window ---
        budget_hi = intent.budget_max or 75
        end = at + timedelta(minutes=budget_hi)
        airport = None
        if intent.deadline == "flight":
            airport = self.locator.airport()
            if airport:
                to_air = travel.taxi_min(anchor.lat, anchor.lon, airport.lat, airport.lon)
                free = budget_hi - to_air - buffer
                ans.notes.append(f"До вылета {budget_hi} мин: дорога в аэропорт ~{to_air} мин на такси и "
                                 f"~{buffer} мин на регистрацию — свободного времени ~{max(free, 0)} мин.")
                if free < 15:
                    ans.notes.append("Если это время до вылета — лучше сразу ехать в аэропорт. "
                                     f"Если {budget_hi} мин — это свободное время до выезда, вот что успеете:")
                    airport = None  # second reading of the request: plain free time
                else:
                    budget_hi = free
                    end = at + timedelta(minutes=free)
        elif intent.deadline == "time" and intent.deadline_time:
            dl = datetime.combine(at.date(), intent.deadline_time, tzinfo=settings.TZ)
            if dl <= at:
                dl += timedelta(days=1)
            budget_hi = min(budget_hi, _mins(dl - at))
            end = min(end, dl)

        scored, rejects, radius, speed = self._candidates(intent, anchor, at, end, budget_hi, weather, airport,
                                                          group, buffer)
        if surprise and scored:
            top = sorted(scored, key=lambda s: -s.score)[:6]
            rng = random.Random(seed if seed is not None else int(at.timestamp()) // 60)
            picked = [rng.choices(top, weights=[math.exp(5 * s.score) for s in top])[0]]
        else:
            picked = self._pick(scored)
        if not picked and not surprise and not group and airport is None and ASSUME_WALK in intent.assumptions:
            # nothing within walking reach (the nearest stand-up is 3 km away): the same request by taxi
            # before giving up, since walking was only our guess
            intent.mode = "taxi"
            by_taxi = self._candidates(intent, anchor, at, end, budget_hi, weather, airport, group, buffer)
            if (taxi_picked := self._pick(by_taxi[0])):
                scored, rejects, radius, speed = by_taxi
                picked = taxi_picked
                intent.assumptions = [a for a in intent.assumptions if a != ASSUME_WALK]
                ans.chips = self._chips(intent, anchor, buffer)
                ans.notes.append("Пешком рядом ничего подходящего — вот что есть на такси.")
            else:
                intent.mode = "walk"
        if not picked:
            ans.notes.append("Рядом не нашлось подходящего с учётом времени, часов работы и погоды. "
                             "Попробуйте увеличить время или поехать на такси.")
        ans.items = [self._item(s, intent, scored, end, at, weather, group) for s in picked]
        if group:
            ans.group_notes.append("Выбираю так, чтобы никто не остался недоволен: важнее самый недовольный, чем средний.")
        if picked and not surprise and (intent.want_route or budget_hi >= 60):
            # the route is the answer in order, not another set of places: one plan on the screen
            ans.route = build_route(picked, anchor, at, end, kids=intent.with_kids, wheelchair=intent.wheelchair,
                                    weather=weather)
        ans.rejected = self._why_not(rejects, scored, picked, intent, radius, surprise=surprise)
        ans.funnel = self._funnel(scored, rejects, len(ans.items))
        ans.context = self._context(radius, speed, end, at, weather)
        self._used_data(ans, anchor, weather, intent)
        self.picked = picked
        if self.narrator is not None and ans.items:
            self.narrator(ans, picked)
        if anchor.source == "gps":  # exact coordinates never leave this function
            anchor.lat, anchor.lon, _ = coarse(anchor.lat, anchor.lon)
            intent.lat, intent.lon = anchor.lat, anchor.lon
        return ans

    # ---------------------------------------------------------------- chips
    def _chips(self, it: Intent, anchor: Anchor | None, buffer: int) -> list[Chip]:
        chips: list[Chip] = []
        if anchor:
            chips.append(Chip(key="anchor", label=anchor.label, kind="place"))
        if it.budget_max:
            lo, hi = it.budget_min or it.budget_max, it.budget_max
            chips.append(Chip(
                key="budget", label=fmt_budget(lo, hi), kind="time",
                assumed=ASSUME_TIME in it.assumptions or ASSUME_FLIGHT in it.assumptions,
                options=[ChipOption(label=lbl, patch={"budget_min": a, "budget_max": b}, selected=b == hi)
                         for lbl, a, b in BUDGETS]))
        chips.append(Chip(
            key="mode", label={"walk": "пешком", "taxi": "на такси", "any": "пешком или на такси"}[it.mode],
            kind="mode", assumed=ASSUME_WALK in it.assumptions,
            options=[ChipOption(label="пешком", patch={"mode": "walk"}, selected=it.mode == "walk"),
                     ChipOption(label="на такси", patch={"mode": "taxi"}, selected=it.mode == "taxi")]))
        chips.append(Chip(key="kids", label="с ребёнком", kind="who", active=it.with_kids,
                          options=[ChipOption(label="без ребёнка" if it.with_kids else "с ребёнком",
                                              patch={"with_kids": not it.with_kids})]))
        chips.append(Chip(key="wheelchair", label="без ступенек", kind="who", active=it.wheelchair,
                          options=[ChipOption(label="обычный маршрут" if it.wheelchair else "без ступенек",
                                              patch={"wheelchair": not it.wheelchair})]))
        if it.group_size > 1:
            chips.append(Chip(key="group", label=f"{it.group_size} чел.", kind="who"))
        if it.deadline == "flight":
            chips.append(Chip(key="deadline", label="до вылета", kind="deadline"))
            chips.append(Chip(key="airport_buffer", label=f"регистрация {buffer} мин", kind="assumption",
                              assumed=True,
                              options=[ChipOption(label="внутренний рейс, 60 мин", patch={"airport_buffer_min": 60},
                                                  selected=buffer == 60),
                                       ChipOption(label="международный, 90 мин", patch={"airport_buffer_min": 90},
                                                  selected=buffer == 90)]))
        elif it.deadline_time:
            chips.append(Chip(key="deadline", label=f"до {it.deadline_time:%H:%M}", kind="deadline"))
        stroll = ASSUME_STROLL in it.assumptions
        for n in it.needs:
            if n in ("kids", "accessible"):
                continue
            chips.append(Chip(key=f"need:{n}", label=K.NEED_LABELS[n], kind="need", assumed=stroll,
                              options=[ChipOption(label="убрать", patch={"remove_needs": [n]})]))
        chips.append(Chip(key="only_events", label="только события", kind="filter", active=it.only_events,
                          options=[ChipOption(label="все места" if it.only_events else "только события",
                                              patch={"only_events": not it.only_events})]))
        return chips

    def _used_data(self, ans: Answer, anchor: Anchor, weather, it: Intent) -> None:
        where = {"gps": "ваша геолокация (округлена до ~150 м, не сохраняется)",
                 "photo": f"{anchor.label} (по фото, само фото не сохраняется)"}.get(anchor.source, anchor.label)
        wsrc = weather.source if weather else "нет данных — учитывать не могу"
        ans.used_data = [
            f"место: {where}",
            f"время: {WEEKDAYS[ans.at.weekday()]} {ans.at:%H:%M}",
            f"погода: {wsrc}",
            "фильтры: " + (", ".join(K.NEED_LABELS[n] for n in it.needs) or "нет"),
            "данные: OpenStreetMap (© участники OSM) и афиши — ссылка у каждого пункта",
        ]
        if weather is not None and weather.air is not None and not weather.simulated:
            ans.used_data.insert(3, f"воздух: {weather.air.source}")

    # ----------------------------------------------------------- candidates
    def _candidates(self, it: Intent, anchor: Anchor, at: datetime, end: datetime, budget: int,
                    weather: W.Weather | None, airport: PlaceLite | None, group: list[Member],
                    buffer: int) -> tuple[list[Scored], list[Reject], float, float]:
        speed = travel.walk_speed_kmh(kids=it.with_kids, wheelchair=it.wheelchair, weather=weather)
        one_way = min(budget * 0.45, 30.0)
        if weather is not None:
            one_way = min(one_way, float(weather.max_outdoor_min))
        radius = 6000.0 if it.mode == "taxi" else max(400.0, one_way * speed * 1000 / 60 / travel.DETOUR)
        cold_cap = weather is not None and it.mode != "taxi" and weather.max_outdoor_min < min(budget * 0.45, 30.0)
        look = radius * 1.6  # a bit further, to be able to say "this one is too far"

        cands: dict[str, K.Cand] = {}
        rows = self.data.places_near(anchor.lat, anchor.lon, look, at, 500)
        closed_malls = [r for r in rows if r["subcategory"] == "mall" and r["open_state"] == "closed"]
        for r in rows:
            if r["category"] == "anchor":  # a university or an airport is where you are, not where to go
                continue
            if r["subcategory"] == "playground" and not (it.with_kids or "playground" in it.needs):
                continue
            c = cands[r["place_id"]] = cand_from_place(r, it.lang)
            if c.open_state == "unknown" and c.category in ("food", "shopping", "entertainment", "nightlife"):
                # a café with unknown hours 30 m from the centre of a mall that is closed now is inside it
                c.inside = next((m for m in closed_malls if m["place_id"] != c.id and travel.haversine_m(
                    m["lat"], m["lon"], c.lat, c.lon) <= (250 if "level" in c.attrs else 120)), None)
        late = not 6 <= at.astimezone(settings.TZ).hour < 22
        for e in self.data.whats_on(anchor.lat, anchor.lon, look, at, max(budget, 60)):
            if (e["kind"] == "kids" and not it.with_kids) or not event_kind_fits(e, it.event_kinds):
                continue  # a kids' show is no answer to "хочу на концерт", like a playground is not
            if e.get("place_id") and e["place_id"] in cands:
                # what is on there counts only if it fits the free time: not "в 18:00 начнётся" at 01:30
                if e["state"] not in ("now", "soon") and (e["starts_at"] > end + timedelta(minutes=30)
                                                          or (late and e["state"] == "today")):
                    continue
                cands[e["place_id"]].attached.append(e)
            else:
                cands["ev:" + e["event_id"]] = K.Cand(
                    kind="event", id=e["event_id"], title=e["title"], lat=e["lat"], lon=e["lon"], category="event",
                    subcategory=e["kind"], indoor="unknown", event=e, description=e.get("description"),
                    source_url=e.get("url"), confidence=e["confidence"])
        for pid in self._anchor_ids(anchor, cands):
            if not self._worth_staying(cands[pid], it, weather):
                del cands[pid]  # "go to Khan Shatyr" is no advice to someone standing in Khan Shatyr

        scored: list[Scored] = []
        rejects: list[Reject] = []
        for c in cands.values():
            s = self._score(c, it, anchor, at, end, budget, weather, airport, group, radius, buffer, cold_cap)
            (scored if isinstance(s, Scored) else rejects).append(s)
        return scored, rejects, radius, speed

    def _anchor_ids(self, anchor: Anchor, cands: dict[str, K.Cand]) -> list[str]:
        """The named place and its OSM copies on the same spot ("Мега Силк Вей" next to "Mega Silk Way")."""
        if not anchor.place_id or (me := self.locator.places.get(anchor.place_id)) is None:
            return []
        names = set().union(*(key_set(n) for n in (me.name, *me.aliases)))
        out = []
        for pid, c in cands.items():
            p = self.locator.places.get(pid)
            if pid == me.id or (p is not None and travel.haversine_m(me.lat, me.lon, p.lat, p.lon) < 200
                                and names & set().union(*(key_set(n) for n in (p.name, *p.aliases)))):
                out.append(pid)
        return out

    @staticmethod
    def _worth_staying(c: K.Cand, it: Intent, weather: W.Weather | None) -> bool:
        """The place the person is at stays in the answer only when it is news or the answer itself:
        something on inside right now, a museum to go into when culture is asked, a mall to walk in
        when it is −25 outside. Not "Хан Шатыр — прямо здесь" for a quiet coffee."""
        if any(e["state"] in ("now", "soon") for e in c.attached):
            return True
        if not K.kind_fit(c, it.event_kinds):
            return False
        first = K.need_match(c, it.needs[0])[0] if it.needs else "unknown"
        if c.category in ("culture", "entertainment") or (c.subcategory in ("attraction", "viewpoint")
                                                           and c.indoor in ("indoor", "mixed")):
            return first in ("yes", "likely")
        if c.category == "landmark":
            return first == "yes"
        if c.subcategory == "mall":
            return weather is not None and weather.outdoor_penalty >= 0.35 and bool({"walk", "indoor"} & set(it.needs))
        return False

    def _score(self, c: K.Cand, it: Intent, anchor: Anchor, at: datetime, end: datetime, budget: int,
               weather: W.Weather | None, airport: PlaceLite | None, group: list[Member], radius: float,
               buffer: int, cold_cap: bool = False) -> Scored | Reject:
        warnings: list[str] = []
        reasons: list[str] = []
        dist = round(travel.haversine_m(anchor.lat, anchor.lon, c.lat, c.lon))
        walk = travel.walk_min(anchor.lat, anchor.lon, c.lat, c.lon, kids=it.with_kids, wheelchair=it.wheelchair,
                               weather=weather)
        taxi = it.mode == "taxi" and walk > 15
        if taxi:
            walk = travel.taxi_min(anchor.lat, anchor.lon, c.lat, c.lon)

        def no(code: str, reason: str) -> Reject:
            return Reject(cand=c, code=code, reason=reason, walk=walk, distance_m=dist)

        if dist > radius or walk > budget * 0.6:
            if cold_cap and weather is not None and walk <= budget * 0.6:
                return no("cold_walk", f"{walk} мин пешком при «{weather.level}» — комфортно не больше "
                                       f"{weather.max_outdoor_min} мин на улице")
            how = "на такси" if taxi else "пешком"
            return no("far", f"{walk} мин {how} — далеко, если на всё {budget} мин")
        arrive = at + timedelta(minutes=walk)
        window_end = end
        closes_first = False
        if airport is not None:  # must still get to the airport in time from *this* place
            back = travel.taxi_min(c.lat, c.lon, airport.lat, airport.lon)
            window_end = min(window_end, at + timedelta(minutes=(it.budget_max or 120) - back - buffer))
            if window_end <= arrive:
                return no("flight", "не успеете вернуться к аэропорту вовремя")
        if it.only_events and c.kind == "place" and not any(e["state"] in ("now", "soon", "later_today")
                                                             for e in c.attached):
            return no("not_event", "сейчас там нет события")
        if (m := c.inside) is not None:
            return no("closed", f"внутри «{display_name(m, it.lang)}», а он закрыт"
                                + (f", откроется {_when(m['opens_at'], at)}" if m.get("opens_at") else ""))

        start_at: datetime | None = None
        confidence = c.confidence
        if c.kind == "place":
            # an evening exhibition opens a museum that is "closed" by its regular hours
            live = [e for e in c.attached if e["state"] in ("now", "soon") and "date_only" not in (e.get("labels") or [])]
            live_end = max((e["ends_at"] for e in live), default=None)
            if c.open_state == "open":
                if c.closes_at:
                    close = max(c.closes_at, live_end) if live_end else c.closes_at
                    if close < window_end:
                        window_end, closes_first = close, True
            elif c.open_state == "closed" and live:
                first = min(live, key=lambda e: e["starts_at"])
                if first["state"] == "soon":
                    start_at = first["starts_at"]
                    if start_at - arrive > timedelta(minutes=45):
                        return no("wait", f"событие начнётся в {_hm(start_at)} — ждать {_mins(start_at - arrive)} мин")
                if live_end < window_end:
                    window_end, closes_first = live_end, True
            elif c.open_state == "closed":
                if c.opens_at and c.opens_at <= arrive + timedelta(minutes=10):
                    start_at = c.opens_at
                elif c.opens_at:
                    return no("closed", f"закрыто, откроется {_when(c.opens_at, at)}")
                else:
                    return no("closed", "закрыто")
            elif c.subcategory in K.PUBLIC_FREE and c.indoor in ("outdoor", "unknown"):
                pass  # a square, a park, an art object: open space, no opening hours to know
            else:
                warnings.append("часы работы неизвестны — уточните перед выходом")
                confidence *= 0.85
        else:
            e = c.event or {}
            if e["state"] == "now":
                if e["ends_at"] < window_end:
                    window_end, closes_first = e["ends_at"], True
            elif e["state"] in ("soon", "later_today"):
                start_at = e["starts_at"]
                if start_at - arrive > timedelta(minutes=45):
                    return no("wait", f"начнётся в {_hm(start_at)} — ждать {_mins(start_at - arrive)} мин")
                if e["ends_at"] < window_end:
                    window_end, closes_first = e["ends_at"], True
            else:
                warnings.append("время начала уточняйте")

        start = max(arrive, start_at or arrive)
        room = _mins(window_end - start)
        v_lo, v_hi = K.visit_range(c)
        if room < max(10, min(v_lo, 20)):
            if closes_first and room >= 0:
                what = "закончится" if (c.kind == "event" or c.open_state == "closed") else "закроется"
                return no("short", f"{what} в {_hm(window_end)} — успеете только {max(room, 0)} мин")
            return no("short", f"не хватит времени: нужно от {min(v_lo, 20)} мин, а останется {max(room, 0)}")
        visit = (min(v_lo, room), max(min(v_lo, room), min(v_hi, room // 5 * 5)))
        if room < v_lo:
            warnings.append(f"успеете ~{room} мин (обычно {v_lo}–{v_hi})")

        # --- weather ---
        penalty = bonus = 0.0
        outdoorish = c.indoor == "outdoor" or (c.kind == "event" and K.need_match(c, "walk")[0] == "likely")
        if weather is not None:
            op = weather.outdoor_penalty
            if outdoorish:
                aqi = weather.air.european_aqi if weather.air is not None else None
                if aqi is not None and aqi >= 80:  # very poor air: no outdoor walks, whatever was asked
                    return no("weather", f"воздух {weather.air.band} — гулять на улице сейчас вредно")  # type: ignore[union-attr]
                if op >= 0.85:  # a blizzard or −30: a walk outside is not advice, whatever was asked
                    return no("weather", f"на улице «{weather.level}», ощущается {weather.feels_c:+.0f}° — "
                                         "сейчас только места под крышей")
                if op >= 0.6 and not ({"park", "walk", "view"} & set(it.needs)):
                    smog = weather.air is not None and weather.air.penalty >= 0.2 and weather.feels_c > -12
                    return no("weather", f"воздух {weather.air.band} — сегодня не для улицы" if smog and weather.air
                              else f"на улице «{weather.level}», ощущается {weather.feels_c:+.0f}° — не для улицы сегодня")
                penalty += 0.35 * op
                if op >= 0.35:
                    warnings.append(f"на улице «{weather.level}» — оденьтесь теплее")
                if weather.is_dark(start) and c.subcategory in {"park", "garden", "viewpoint"} and "night" not in it.needs:
                    penalty += 0.1
                    warnings.append("уже стемнеет")
            elif c.indoor == "mixed":
                penalty += 0.15 * op
            elif c.indoor == "indoor" and op >= 0.35:
                bonus += 0.06
                reasons.append(f"в помещении, а на улице «{weather.level}»")
            if walk > weather.max_outdoor_min and it.mode != "taxi":
                return no("cold_walk", f"{walk} мин пешком при «{weather.level}» — комфортно не больше "
                                       f"{weather.max_outdoor_min} мин")
            if weather.golden_hour(start + timedelta(minutes=5)) and not K.indoor_attraction(c) and (
                    c.subcategory in K.PHOTO_SUBS or c.attrs.get("view") == "yes"):
                reasons.append(f"успеете к золотому часу: закат в {_hm(weather.sunset)}")  # type: ignore[arg-type]
                if {"photo", "view", "romantic"} & set(it.needs):
                    bonus += 0.05

        # --- hard needs ---
        if it.with_kids and K.need_match(c, "kids", at=start)[0] == "no":
            return no("kids", "не для детей")
        if it.wheelchair:
            access = K.need_match(c, "accessible", at=start)[0]
            if access == "no":
                return no("wheelchair", "по OpenStreetMap недоступно для колясок")
            if access == "unknown":   # most places: say so rather than send someone to a staircase
                warnings.append("доступность для колясок не отмечена — уточните, есть ли ступени и лифт")

        # --- match the request ---
        cold = weather is not None and weather.outdoor_penalty >= 0.35
        hits = {n: K.need_match(c, n, at=start, cold=cold) for n in it.needs}
        if group:
            need_score, group_expl = group_score(c, group, start)
        else:
            group_expl = []
            tot = wsum = 0.0
            for i, n in enumerate(it.needs):
                w = 1.6 if i == 0 else 1.0
                tot += w * K.WEIGHT[hits[n][0]]
                wsum += w
            need_score = tot / wsum if wsum else 0.5

        # --- what is on there ---
        ev_bonus = 0.0
        for e in ([c.event] if c.event else []) + c.attached:
            labels = set(e.get("labels") or [])
            b = {"now": 0.22, "soon": 0.14, "later_today": 0.05, "today": 0.03}.get(e["state"], 0.0)
            b += 0.05 if labels & {"temporary", "today_only"} else 0.0
            b += 0.05 if "last_days" in labels else 0.0
            ev_bonus = max(ev_bonus, b * e["confidence"])
        if c.attached:
            e = sorted(c.attached, key=lambda x: {"now": 0, "soon": 1}.get(x["state"], 2))[0]
            if e["state"] == "now":
                reasons.insert(0, f"сейчас там идёт: {e['title']}")
            elif e["state"] in ("soon", "later_today"):
                reasons.insert(0, f"в {_hm(e['starts_at'])} там начнётся: {e['title']}")

        # an event on there counts when the person is after an evening out, not when they asked for a park walk
        # or a quiet coffee: then a concert in a café is a side note, not a reason to send them 20 minutes away
        if (not any(a.startswith("просто погулять") for a in it.assumptions) and not it.event_kinds
                and not set(it.needs) & {"event", "unusual", "culture", "night", "nightlife", "romantic", "kids"}):
            ev_bonus *= 0.3
        if it.persona == "tourist" and c.category in ("landmark", "culture"):
            bonus += 0.08
        if not K.kind_fit(c, it.event_kinds):  # "хочу на выставку": a theatre is culture, but not that
            penalty += 0.25
        # the first thing asked for matters most: "кофе и розетка" -> a museum is not an answer,
        # unless something else asked for is clearly there
        explicit = not any(a.startswith("просто погулять") for a in it.assumptions)
        if (explicit and it.needs and hits[it.needs[0]][0] in ("unknown", "no") and ev_bonus < 0.1
                and not any(v[0] == "yes" for v in hits.values())):
            penalty += 0.2
        # "тихо + кофе + розетка": a place that covers only one of three is not the answer
        covered = sum(1 for v in hits.values() if v[0] in ("yes", "likely"))
        if explicit and len(it.needs) >= 2 and covered / len(it.needs) < 0.5 and ev_bonus < 0.1:
            penalty += 0.15
        # late at night "activity" means somewhere open and alive, not a dark square
        late = start.astimezone(settings.TZ).hour >= 22 or start.astimezone(settings.TZ).hour < 5
        if "night" in it.needs and late and outdoorish:
            penalty += 0.15
            warnings.append("ночью на улице — по настроению")
        dist_pen = 0.25 * walk / max(budget, 20)
        score = 0.62 * need_score + ev_bonus + bonus - dist_pen - penalty + 0.05 * confidence
        if need_score < 0.22 and ev_bonus < 0.1:
            asked = ", ".join(K.NEED_LABELS[n] for n in it.needs[:2])
            return no("fit", f"не про то, что вы просили: {asked}")

        for n in it.needs:
            cert, evd = hits[n]
            if cert == "yes" and evd:
                reasons.append(f"{K.NEED_LABELS[n]}: {evd}")
            elif cert == "likely" and evd:
                reasons.append(f"{K.NEED_LABELS[n]} — вероятно ({evd})")
        if walk <= 5:
            reasons.append("в паре минут" if walk else "прямо здесь")
        return Scored(cand=c, walk=walk, arrive=arrive, start_at=start_at, window_end=window_end, visit=visit,
                      score=round(score, 4), need_score=round(need_score, 3), hits=hits, reasons=reasons,
                      warnings=warnings, confidence=round(confidence, 2), group_expl=group_expl,
                      distance_m=dist, closes_first=closes_first, taxi=taxi)

    def _pick(self, scored: list[Scored], k: int = 3) -> list[Scored]:
        pool = sorted(scored, key=lambda s: -s.score)
        if not pool:
            return []
        floor = max(0.15, pool[0].score - 0.4)
        chosen: list[Scored] = []
        while pool and len(chosen) < k:
            def adj(s: Scored) -> float:
                same_sub = sum(1 for x in chosen if x.cand.subcategory == s.cand.subcategory)
                same_cat = sum(1 for x in chosen if x.cand.category == s.cand.category)
                # two "Детская площадка" 30 m apart are one answer, not two
                twin = any(x.cand.title.casefold() == s.cand.title.casefold() for x in chosen)
                return s.score - 0.15 * same_sub - 0.04 * same_cat - (1.0 if twin else 0.0)
            best = max(pool, key=adj)
            if adj(best) < floor:
                break
            chosen.append(best)
            pool.remove(best)
        return chosen

    # ---------------------------------------------------------------- output
    def _labels(self, s: Scored, start: datetime) -> list[str]:
        c = s.cand
        out: list[str] = []
        evs = ([c.event] if c.event else []) + [e for e in c.attached if e["state"] in ("now", "soon", "later_today")]
        for e in evs[:1]:
            out.append({"now": "now", "soon": "soon", "later_today": "later_today"}.get(e["state"], "today"))
            out += [lb for lb in (e.get("labels") or []) if lb in {"evening", "night", "temporary", "today_only",
                                                                     "last_days", "free", "ends_soon"}]
        if c.attached:
            out.append("event_inside")
        if c.kind == "place":
            if c.subcategory in K.PUBLIC_FREE and c.indoor in ("outdoor", "unknown") and c.open_state == "unknown":
                out.append("open_space")
            elif c.open_state == "unknown":
                out.append("hours_unknown")
            elif s.closes_first and s.window_end and s.window_end - start <= timedelta(minutes=45):
                out.append("closes_soon")
        if c.indoor in ("indoor", "outdoor"):
            out.append(c.indoor)
        return list(dict.fromkeys(out))

    def _item(self, s: Scored, it: Intent, scored: list[Scored], end: datetime, at: datetime,
              weather: W.Weather | None, group: list[Member]) -> Item:
        c = s.cand
        start = max(s.arrive, s.start_at or s.arrive)
        if c.kind == "event":
            status = describe_event(c.event or {})
        elif c.open_state == "open" and c.closes_at:
            status = "открыто круглосуточно" if c.closes_at - start > timedelta(hours=20) else f"открыто до {_hm(c.closes_at)}"
        elif c.open_state == "closed" and c.opens_at:
            status = f"откроется в {_hm(c.opens_at)}"
        elif c.subcategory in K.PUBLIC_FREE and c.indoor in ("outdoor", "unknown"):
            status = "открытое пространство, вход свободный"
        else:
            status = "часы работы неизвестны"
        live = sorted((e for e in c.attached if e["state"] in ("now", "soon")),
                      key=lambda x: {"now": 0, "soon": 1}[x["state"]])
        if live:  # what is happening there matters more than regular hours
            status = f"{live[0]['title']}: {describe_event(live[0])}"
        what, src = K.idea(c)
        # "затем кофе рядом" — like a local would suggest
        if c.subcategory not in {"cafe", "restaurant", "fast_food"} and "coffee" not in it.needs[:1]:
            leave = start + timedelta(minutes=s.visit[0])
            cafes = [x for x in scored if x.cand.subcategory == "cafe" and x.cand.id != c.id
                     and travel.haversine_m(c.lat, c.lon, x.cand.lat, x.cand.lon) <= 250
                     and (x.window_end is None or x.window_end - leave >= timedelta(minutes=25))]
            if cafes and end - leave >= timedelta(minutes=25):
                near = min(cafes, key=lambda x: travel.haversine_m(c.lat, c.lon, x.cand.lat, x.cand.lon))
                m = travel.walk_min(c.lat, c.lon, near.cand.lat, near.cand.lon)
                what += f"; затем кофе рядом — {near.cand.title} ({max(m, 1)} мин)"
        # "выходите до 18:40" — only when it matters
        leave_by = None
        starts_at = s.start_at
        soon = [e["starts_at"] for e in c.attached if e["state"] == "soon" and e["starts_at"] > s.arrive]
        if starts_at is None and soon:
            starts_at = min(soon)  # the jazz starts at 20:00 in a bar that is already open
        horizon = 45
        if starts_at is not None and starts_at > s.arrive:
            leave_by, horizon = starts_at - timedelta(minutes=s.walk), 120   # "к началу" is always useful
        elif s.closes_first and s.window_end is not None:
            leave_by = s.window_end - timedelta(minutes=s.visit[0] + s.walk)
        if leave_by is not None:
            leave_by = max(leave_by, at)
            if leave_by - at > timedelta(minutes=horizon):
                leave_by = None
        sources = [u for u in [c.source_url] + [e.get("url") for e in c.attached] if u]
        return Item(
            kind=c.kind, title=c.title, place_id=c.id if c.kind == "place" else (c.event or {}).get("place_id"),
            event_id=c.id if c.kind == "event" else None, category=c.category, subcategory=c.subcategory,
            lat=c.lat, lon=c.lon, distance_m=s.distance_m, walk_min=s.walk, by="taxi" if s.taxi else "walk",
            arrive_at=start, visit_min=s.visit,
            status=status, labels=self._labels(s, start), why=s.reasons[:4], group_fit=s.group_expl,
            group_detail=group_detail(c, group, start) if group else [], what_to_do=what, what_to_do_source=src,
            confidence=s.confidence, sources=sources, warnings=s.warnings,
            attached_event=c.attached[0]["title"] if c.attached else None, score=s.score, indoor=c.indoor,
            need_hits={n: v[0] for n, v in s.hits.items()}, starts_at=starts_at,
            window_end=s.window_end, leave_by=leave_by,
        )

    def _why_not(self, rejects: list[Reject], scored: list[Scored], picked: list[Scored], it: Intent,
                 radius: float, limit: int = 8, surprise: bool = False) -> list[Rejected]:
        def as_rejected(c: K.Cand, code: str, reason: str, walk: int, dist: int) -> Rejected:
            return Rejected(title=c.title, kind=c.kind, place_id=c.id if c.kind == "place" else (c.event or {}).get("place_id"),
                            event_id=c.id if c.kind == "event" else None, category=c.category,
                            subcategory=c.subcategory, lat=c.lat, lon=c.lon, distance_m=dist, walk_min=walk,
                            reason_code=code, reason=reason)  # type: ignore[arg-type]

        out: list[Rejected] = []
        if picked:
            best = picked[0]
            for s in sorted((s for s in scored if s not in picked), key=lambda s: -s.score)[:3]:
                twin = next((p for p in picked if p.cand.subcategory == s.cand.subcategory), None)
                first = it.needs[0] if it.needs else None
                if surprise:
                    why = "тоже был в шорт-листе сюрприза — выпало другое"
                elif twin is not None and abs(s.need_score - twin.need_score) < 0.15:
                    why = f"похожее место уже в подборке: «{twin.cand.title}»"
                elif s.need_score + 0.05 < best.need_score:
                    lack = (f" ({K.NEED_LABELS[first]} — {'неизвестно' if s.hits[first][0] == 'unknown' else 'нет'})"
                            if first and s.hits.get(first, ("yes",))[0] in ("unknown", "no") else "")
                    why = f"подходит хуже, чем «{best.cand.title}»{lack}"
                elif s.walk > best.walk + 3:
                    why = (f"дальше: {s.walk} мин пешком, а «{best.cand.title}» прямо здесь" if best.walk == 0
                           else f"дальше: {s.walk} мин пешком против {best.walk} до «{best.cand.title}»")
                else:
                    why = "в запасе: чуть ниже по сумме — расстояние, события, погода"
                out.append(as_rejected(s.cand, "outranked", why, s.walk, s.distance_m))
        interesting = [r for r in rejects if r.code != "far" or r.distance_m <= radius * 1.6]
        interesting.sort(key=lambda r: (-relevance(r.cand, list(it.needs)), r.distance_m))
        for r in interesting:
            if len(out) >= limit:
                break
            if relevance(r.cand, list(it.needs)) < 0.45 and r.distance_m > 300:
                continue
            out.append(as_rejected(r.cand, r.code, r.reason, r.walk, r.distance_m))
        return out

    @staticmethod
    def _funnel(scored: list[Scored], rejects: list[Reject], picked: int) -> list[FunnelStep]:
        total = len(scored) + len(rejects)
        by = {"reach": 0, "open": 0, "fit": 0}
        for r in rejects:
            by[STAGE.get(r.code, "fit")] += 1
        reach = total - by["reach"]
        open_ = reach - by["open"]
        return [FunnelStep(key="nearby", label="мест рядом", n=total),
                FunnelStep(key="reachable", label="успеете дойти", n=reach),
                FunnelStep(key="open", label="открыто к приходу", n=open_),
                FunnelStep(key="fit", label="подходят под запрос", n=len(scored)),
                FunnelStep(key="picked", label="в ответе", n=picked)]

    @staticmethod
    def _context(radius: float, speed: float, end: datetime, at: datetime, weather: W.Weather | None) -> Context:
        ctx = Context(radius_m=round(radius), walk_kmh=round(speed, 2), window_end=end)
        if weather is not None:
            level, label, factors = weather.comfort(at)
            ctx.weather = WeatherInfo(
                temp_c=weather.temp_c, feels_c=weather.feels_c, wind_ms=weather.wind_ms, level=weather.level,
                precip=weather.precip_now, code=weather.code, summary=weather.summary(), source=weather.source,
                simulated=bool(weather.simulated), sunrise=weather.sunrise, sunset=weather.sunset,
                dark=weather.is_dark(at), golden_hour=weather.golden_hour(at))
            ctx.comfort = Comfort(level=level, label=label, factors=factors)
            ctx.simulated = weather.simulated
            if weather.air is not None:
                ctx.air = AirInfo(pm2_5=weather.air.pm2_5, pm10=weather.air.pm10,
                                  european_aqi=weather.air.european_aqi, band=weather.air.band,
                                  source=weather.air.source)
        return ctx
