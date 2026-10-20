"""schema.org Event extraction from JSON-LD blocks — the most reliable source when a site has it."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from dateutil import parser as dtparser

from .. import settings
from .models import ExtractedEvent

log = logging.getLogger(__name__)

TYPE_TO_KIND = {
    "ExhibitionEvent": "exhibition", "VisualArtsEvent": "exhibition", "MusicEvent": "concert",
    "TheaterEvent": "performance", "DanceEvent": "performance", "Festival": "festival",
    "ComedyEvent": "standup", "ChildrensEvent": "kids", "EducationEvent": "workshop",
    "SportsEvent": "sport", "ScreeningEvent": "film", "LiteraryEvent": "lecture",
    "SocialEvent": "party", "BusinessEvent": "lecture", "FoodEvent": "fair", "SaleEvent": "fair",
    "Event": "other",
}
_TAG_RE = re.compile(r"<[^>]+>")


def _types(node: dict) -> list[str]:
    t = node.get("@type")
    t = t if isinstance(t, list) else [t]
    return [x.split("/")[-1] for x in t if isinstance(x, str)]


def _walk(node):
    if isinstance(node, list):
        for x in node:
            yield from _walk(x)
    elif isinstance(node, dict):
        yield node
        for key in ("@graph", "subEvent", "subEvents", "event", "events", "itemListElement", "item"):
            if key in node:
                yield from _walk(node[key])


def _text(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, list):
        v = v[0] if v else None
    if isinstance(v, dict):
        v = v.get("name") or v.get("@value") or v.get("url")
    if v is None:
        return None
    t = _TAG_RE.sub(" ", str(v))
    return re.sub(r"\s+", " ", t).strip() or None


def _split_dt(v) -> tuple[str | None, str | None]:
    """ISO datetime -> (YYYY-MM-DD, HH:MM) in Astana time; date-only stays without time."""
    s = _text(v)
    if not s:
        return None, None
    try:
        dt = dtparser.isoparse(s)
    except (ValueError, OverflowError):
        try:
            dt = dtparser.parse(s, dayfirst=True)
        except (ValueError, OverflowError):
            return None, None
    has_time = "T" in s or re.search(r"\d{1,2}:\d{2}", s) is not None
    if isinstance(dt, datetime) and dt.tzinfo is not None:
        dt = dt.astimezone(settings.TZ)
    return dt.date().isoformat(), (dt.strftime("%H:%M") if has_time else None)


def _location(loc) -> tuple[str | None, str | None, float | None, float | None]:
    if isinstance(loc, list):
        loc = loc[0] if loc else None
    if loc is None:
        return None, None, None, None
    if isinstance(loc, str):
        return loc.strip() or None, None, None, None
    name = _text(loc.get("name"))
    addr = loc.get("address")
    if isinstance(addr, dict):
        addr = ", ".join(x for x in (addr.get("streetAddress"), addr.get("addressLocality")) if x) or None
    else:
        addr = _text(addr)
    lat = lon = None
    geo = loc.get("geo")
    if isinstance(geo, dict):
        try:
            lat, lon = float(geo.get("latitude")), float(geo.get("longitude"))
        except (TypeError, ValueError):
            lat = lon = None
    return name, addr, lat, lon


_STREET_RE = re.compile(r"^(пр|ул|просп|проспект|улица|мкр|микрорайон|ш|шоссе|б-р|бульвар|наб|г|ж\.?м)\b\.?", re.I)


def _real_venue(venue: str | None, addr: str | None, title: str) -> tuple[str | None, str | None]:
    """Some sites (sxodim) put the event title into location.name and the venue into the address:
    "Дворец единоборств имени Ж. Ушкемпирова, пр. Кабанбай батыра, 43, Астана"."""
    if venue and venue.casefold().strip() != title.casefold().strip():
        return venue, addr
    head, sep, rest = (addr or "").partition(",")
    head = head.strip()
    if sep and head and not re.search(r"\d", head) and not _STREET_RE.match(head):
        return head, rest.strip() or None
    return None, addr


def _offers(offers) -> tuple[float | None, str | None]:
    items = offers if isinstance(offers, list) else [offers] if offers else []
    prices: list[float] = []
    cur = None
    for o in items:
        if not isinstance(o, dict):
            continue
        for k in ("price", "lowPrice"):
            try:
                if o.get(k) not in (None, ""):
                    prices.append(float(str(o[k]).replace(" ", "").replace(",", ".")))
            except ValueError:
                pass
        cur = cur or o.get("priceCurrency")
    return (min(prices) if prices else None), cur


def _status(node: dict) -> str:
    s = str(node.get("eventStatus") or "")
    if "Cancelled" in s:
        return "cancelled"
    if "Postponed" in s:
        return "postponed"
    return "scheduled"


def _image(v) -> str | None:
    if isinstance(v, list):
        v = v[0] if v else None
    if isinstance(v, dict):
        v = v.get("url") or v.get("contentUrl")
    return v if isinstance(v, str) else None


def extract(html: str, base_url: str) -> list[ExtractedEvent]:
    soup = BeautifulSoup(html, "lxml")
    out: list[ExtractedEvent] = []
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = script.string or script.get_text() or ""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            try:  # common breakage: trailing commas
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", raw))
            except json.JSONDecodeError:
                log.debug("bad JSON-LD on %s", base_url)
                continue
        for node in _walk(data):
            types = _types(node)
            kind = next((TYPE_TO_KIND[t] for t in types if t in TYPE_TO_KIND), None)
            if not kind or not node.get("name"):
                continue
            d1, t1 = _split_dt(node.get("startDate"))
            d2, t2 = _split_dt(node.get("endDate"))
            venue, addr, lat, lon = _location(node.get("location"))
            venue, addr = _real_venue(venue, addr, _text(node.get("name")) or "")
            price, _cur = _offers(node.get("offers"))
            free = node.get("isAccessibleForFree")
            if free is None and price is not None:
                free = price == 0
            if d2 == d1 and t2 is None:
                d2 = None
            desc = _text(node.get("description"))
            out.append(ExtractedEvent(
                title=_text(node.get("name")) or "",
                kind=kind,
                venue_name=venue,
                address=addr,
                lat=lat,
                lon=lon,
                date_start=d1,
                date_end=d2 if (d2 and d2 != d1) else None,
                time_start=t1,
                time_end=t2 if (d2 in (None, d1)) else None,
                price_min=price,
                price_text=(f"от {price:g} ₸" if price else None),
                is_free=bool(free) if free is not None else None,
                age_limit=_text(node.get("typicalAgeRange")),
                description=(desc[:400] if desc else None),
                status=_status(node),
                evidence=f"startDate={_text(node.get('startDate'))}; endDate={_text(node.get('endDate'))}",
                url=urljoin(base_url, _text(node.get("url")) or base_url),
                image_url=_image(node.get("image")),
            ))
    return out
