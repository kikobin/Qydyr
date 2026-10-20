"""Event data models: what extractors return and what goes into the database."""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

Kind = Literal[
    "exhibition", "concert", "performance", "festival", "fair", "workshop", "lecture",
    "party", "excursion", "sport", "kids", "film", "standup", "other",
]
Status = Literal["scheduled", "cancelled", "postponed"]
Extraction = Literal["jsonld", "llm", "telegram", "manual"]


class LLMSession(BaseModel):
    date: str = Field(description="YYYY-MM-DD")
    time_start: Optional[str] = Field(default=None, description="HH:MM, 24h, Astana time")
    time_end: Optional[str] = Field(default=None, description="HH:MM, 24h")


class LLMEvent(BaseModel):
    """Schema the LLM must fill. Unknown fields stay null — nothing is invented."""
    title: str
    kind: Kind = "other"
    venue_name: Optional[str] = None
    address: Optional[str] = None
    date_start: Optional[str] = Field(default=None, description="YYYY-MM-DD")
    date_end: Optional[str] = Field(default=None, description="YYYY-MM-DD, for periods")
    time_start: Optional[str] = Field(default=None, description="HH:MM")
    time_end: Optional[str] = Field(default=None, description="HH:MM")
    sessions: list[LLMSession] = Field(default_factory=list)
    schedule_opening_hours: Optional[str] = Field(
        default=None, description='Recurring hours in OSM opening_hours syntax, e.g. "Tu-Su 10:00-18:00"')
    price_text: Optional[str] = None
    price_min: Optional[float] = None
    is_free: Optional[bool] = None
    age_limit: Optional[str] = None
    description: Optional[str] = None
    status: Status = "scheduled"
    evidence: Optional[str] = Field(default=None, description="Verbatim quote with the date/time")


class LLMOutput(BaseModel):
    events: list[LLMEvent] = Field(default_factory=list)


class ExtractedEvent(LLMEvent):
    """LLMEvent plus fields only structured sources (JSON-LD) can give."""
    url: Optional[str] = None
    image_url: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None


class RawEvent(BaseModel):
    """One extraction from one source, before venue matching and dedup."""
    event: ExtractedEvent
    source_id: str
    source_url: str
    extraction: Extraction
    mode: Literal["jsonld", "page", "digest", "post"]
    reference_date: date
    fetched_at: datetime
    reliability: float = 0.8
    venue_hint: Optional[str] = None
    kind_hint: Optional[Kind] = None
    is_free_hint: Optional[bool] = None
    evidence_ok: bool = False
    dates_consistent: Optional[bool] = None  # dates parsed from evidence agree with extracted dates


class Session(BaseModel):
    starts_at: datetime
    ends_at: datetime
    time_known: bool = True
    end_estimated: bool = False
    hours_from: Literal["event", "venue", "none"] = "event"


class EventRecord(BaseModel):
    """Row for the `events` table (+ its sessions)."""
    id: str
    title: str
    description: Optional[str] = None
    kind: Kind = "other"
    is_temporary: bool = False
    place_id: Optional[str] = None
    venue_name: Optional[str] = None
    address: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    starts_on: Optional[date] = None
    ends_on: Optional[date] = None
    schedule_text: Optional[str] = None
    price_text: Optional[str] = None
    price_min: Optional[float] = None
    is_free: Optional[bool] = None
    age_limit: Optional[str] = None
    url: Optional[str] = None
    image_url: Optional[str] = None
    status: Status = "scheduled"
    extraction: Extraction = "llm"
    evidence: Optional[str] = None
    evidence_ok: bool = False
    confidence: float = 0.5
    sources: list[dict] = Field(default_factory=list)
    sessions: list[Session] = Field(default_factory=list)
    match_method: Optional[str] = None
