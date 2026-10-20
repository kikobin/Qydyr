from __future__ import annotations

from datetime import datetime, time
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

Need = Literal[
    "coffee", "food", "socket", "wifi", "quiet", "kids", "playground", "interactive", "museum", "culture",
    "park", "walk", "view", "unusual", "night", "nightlife", "free", "indoor", "accessible", "romantic",
    "photo", "shopping", "event", "cheap", "dog",
]
ALL_NEEDS: tuple[str, ...] = Need.__args__  # type: ignore[attr-defined]
Certainty = Literal["yes", "likely", "unknown", "no"]


class Intent(BaseModel):
    """What the person wants, in a form the ranker can use. Filled by rules and/or the LLM."""
    text: str
    lang: Literal["ru", "kk", "en"] = "ru"
    anchor_text: Optional[str] = None           # "Mega Silk Way", "Чупа-чупс", "AITU"
    lat: Optional[float] = None                 # GPS, if the person shared location
    lon: Optional[float] = None
    budget_min: Optional[int] = None            # minutes, lower bound
    budget_max: Optional[int] = None            # minutes, upper bound
    deadline: Optional[Literal["flight", "time"]] = None
    deadline_time: Optional[time] = None        # "до 18:00"
    start_time: Optional[time] = None           # "вечером", "в 19:00": plan for later, not for now
    start_days: int = 0                         # "завтра" = 1
    start_weekday: Optional[int] = None         # "в субботу" = 5 (Monday = 0)
    mode: Literal["walk", "taxi", "any"] = "walk"
    with_kids: bool = False
    group_size: int = 1
    wheelchair: bool = False
    persona: Optional[Literal["local", "tourist", "student", "family"]] = None
    needs: list[Need] = Field(default_factory=list)
    event_kinds: list[str] = Field(default_factory=list)  # "концерт" -> ["concert"]: which events count
    want_route: bool = False
    only_events: bool = False                   # "только события" switch
    assumptions: list[str] = Field(default_factory=list)  # shown to the person as editable chips


class Overrides(BaseModel):
    """Edits the person makes by tapping chips. Applied on top of what was understood from the text."""
    budget_min: Optional[int] = None
    budget_max: Optional[int] = None
    mode: Optional[Literal["walk", "taxi"]] = None
    with_kids: Optional[bool] = None
    wheelchair: Optional[bool] = None
    add_needs: list[Need] = Field(default_factory=list)
    remove_needs: list[Need] = Field(default_factory=list)
    only_events: Optional[bool] = None
    airport_buffer_min: Optional[int] = None
    lang: Optional[Literal["ru", "kk", "en"]] = None

    def merged(self, patch: dict[str, Any]) -> "Overrides":
        """Apply one chip option's patch; list fields accumulate."""
        data = self.model_dump()
        for k, v in patch.items():
            if k in ("add_needs", "remove_needs"):
                data[k] = list(dict.fromkeys(list(data[k]) + list(v)))
                other = "remove_needs" if k == "add_needs" else "add_needs"
                data[other] = [n for n in data[other] if n not in v]
            else:
                data[k] = v
        return Overrides.model_validate(data)


class Anchor(BaseModel):
    lat: float
    lon: float
    label: str
    source: Literal["gps", "place", "default", "photo", "address"]
    place_id: Optional[str] = None


class ChipOption(BaseModel):
    label: str
    patch: dict[str, Any]
    selected: bool = False


class Chip(BaseModel):
    """'Понял так' — every chip is something the person can tap to change."""
    key: str
    label: str
    kind: Literal["place", "time", "mode", "who", "deadline", "need", "filter", "assumption"]
    active: bool = True          # False: a switch the person can turn on ("с ребёнком", "только события")
    assumed: bool = False        # the system guessed it, not said by the person
    options: list[ChipOption] = Field(default_factory=list)


class FitMark(BaseModel):
    need: str
    label: str
    certainty: Certainty


class MemberFit(BaseModel):
    name: str
    marks: list[FitMark] = Field(default_factory=list)


class Item(BaseModel):
    """One recommendation."""
    kind: Literal["place", "event"]
    title: str
    place_id: Optional[str] = None
    event_id: Optional[str] = None
    category: Optional[str] = None
    subcategory: Optional[str] = None
    lat: float
    lon: float
    distance_m: int
    walk_min: int                                # minutes to get there (by taxi when by == "taxi")
    by: Literal["walk", "taxi"] = "walk"
    arrive_at: datetime
    visit_min: tuple[int, int]
    status: str                                  # "открыто до 22:00" / "Идёт сейчас · вечерняя выставка"
    where: Optional[str] = None                  # how to find it: "ТРЦ Керуен, 3 этаж" (2ГИС)
    rating: Optional[float] = None               # 2ГИС rating, when at least 15 reviews stand behind it
    reviews: Optional[int] = None
    labels: list[str] = Field(default_factory=list)  # machine labels for the UI: now, soon, evening, free...
    why: list[str] = Field(default_factory=list)
    pitch: Optional[str] = None                  # the "why" in one human sentence, written by Claude from these facts
    group_fit: list[str] = Field(default_factory=list)  # group mode: how it fits each person (text)
    group_detail: list[MemberFit] = Field(default_factory=list)
    what_to_do: str = ""
    what_to_do_source: Literal["data", "idea"] = "idea"
    confidence: float = 0.5
    sources: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    attached_event: Optional[str] = None         # an event happening inside this place
    score: float = 0.0
    indoor: str = "unknown"
    need_hits: dict[str, str] = Field(default_factory=dict)  # need -> yes/likely/unknown/no
    starts_at: Optional[datetime] = None         # an event or an opening later than arrival
    window_end: Optional[datetime] = None        # closes / event ends / free time ends
    leave_by: Optional[datetime] = None          # latest time to set off and still make it


class Rejected(BaseModel):
    """'Почему не это?' — a nearby candidate that was considered and dropped, with the reason."""
    title: str
    kind: Literal["place", "event"]
    place_id: Optional[str] = None
    event_id: Optional[str] = None
    category: Optional[str] = None
    subcategory: Optional[str] = None
    lat: float
    lon: float
    distance_m: int
    walk_min: int
    reason_code: Literal["far", "closed", "short", "wait", "weather", "cold_walk", "kids", "wheelchair",
                         "flight", "fit", "outranked", "not_event"]
    reason: str


class FunnelStep(BaseModel):
    key: str
    label: str
    n: int


class WeatherInfo(BaseModel):
    temp_c: float
    feels_c: float
    wind_ms: float
    level: str                    # дубак-о-метр
    precip: bool
    code: int
    summary: str
    source: str
    simulated: bool = False
    sunrise: Optional[datetime] = None
    sunset: Optional[datetime] = None
    dark: bool = False
    golden_hour: bool = False


class AirInfo(BaseModel):
    pm2_5: Optional[float] = None
    pm10: Optional[float] = None
    european_aqi: Optional[float] = None
    band: str
    source: str


class Comfort(BaseModel):
    """Kyoto-style comfort index for being outside right now: 0 = stay in, 4 = perfect."""
    level: int
    label: str
    factors: list[str] = Field(default_factory=list)


class Context(BaseModel):
    radius_m: int = 0             # how far the person can get in the time (the reach circle on the map)
    walk_kmh: float = 4.8
    window_end: Optional[datetime] = None
    weather: Optional[WeatherInfo] = None
    air: Optional[AirInfo] = None
    comfort: Optional[Comfort] = None
    simulated: Optional[str] = None   # "метель −25" when the weather is a what-if preset


class Stop(BaseModel):
    title: str
    arrive_at: datetime
    leave_at: datetime
    walk_min: int
    lat: Optional[float] = None
    lon: Optional[float] = None
    place_id: Optional[str] = None
    kind: str = "place"


class Route(BaseModel):
    stops: list[Stop]
    total_min: int
    walk_min: int
    outdoor_min: int
    geometry: list[list[float]] = Field(default_factory=list)   # [[lon, lat], ...] from the start through stops
    geometry_source: Literal["straight", "osrm"] = "straight"


class Answer(BaseModel):
    id: str = ""
    intent: Intent
    anchor: Optional[Anchor] = None
    at: datetime
    weather: Optional[str] = None
    chips: list[Chip] = Field(default_factory=list)
    used_data: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    items: list[Item] = Field(default_factory=list)
    route: Optional[Route] = None
    group_notes: list[str] = Field(default_factory=list)
    rejected: list[Rejected] = Field(default_factory=list)
    funnel: list[FunnelStep] = Field(default_factory=list)
    context: Context = Field(default_factory=Context)
    surprise: bool = False
    narrated: bool = False                      # some text was written by Claude from the retrieved facts
