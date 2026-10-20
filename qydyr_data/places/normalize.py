"""OSM elements -> normalized places with our category taxonomy and indoor/outdoor flag."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .. import hours

# (osm key, value) -> (category, subcategory, indoor)
CATEGORY_MAP: dict[tuple[str, str], tuple[str, str, str]] = {
    ("amenity", "cafe"): ("food", "cafe", "indoor"),
    ("amenity", "restaurant"): ("food", "restaurant", "indoor"),
    ("amenity", "fast_food"): ("food", "fast_food", "indoor"),
    ("amenity", "food_court"): ("food", "food_court", "indoor"),
    ("amenity", "ice_cream"): ("food", "ice_cream", "indoor"),
    ("amenity", "bar"): ("nightlife", "bar", "indoor"),
    ("amenity", "pub"): ("nightlife", "pub", "indoor"),
    ("amenity", "nightclub"): ("nightlife", "nightclub", "indoor"),
    ("amenity", "cinema"): ("entertainment", "cinema", "indoor"),
    ("amenity", "theatre"): ("culture", "theatre", "indoor"),
    ("amenity", "arts_centre"): ("culture", "arts_centre", "indoor"),
    ("amenity", "library"): ("culture", "library", "indoor"),
    ("amenity", "planetarium"): ("culture", "planetarium", "indoor"),
    ("amenity", "community_centre"): ("culture", "community_centre", "indoor"),
    ("amenity", "coworking_space"): ("work", "coworking", "indoor"),
    ("amenity", "marketplace"): ("shopping", "marketplace", "mixed"),
    ("amenity", "fountain"): ("landmark", "fountain", "outdoor"),
    ("amenity", "concert_hall"): ("culture", "concert_hall", "indoor"),
    ("amenity", "events_venue"): ("culture", "events_venue", "indoor"),
    ("amenity", "exhibition_centre"): ("culture", "exhibition_centre", "indoor"),
    ("amenity", "place_of_worship"): ("landmark", "place_of_worship", "indoor"),
    ("amenity", "hookah_lounge"): ("nightlife", "hookah", "indoor"),
    ("amenity", "karaoke_box"): ("nightlife", "karaoke", "indoor"),
    ("amenity", "biergarten"): ("nightlife", "biergarten", "outdoor"),
    ("tourism", "museum"): ("culture", "museum", "indoor"),
    ("tourism", "gallery"): ("culture", "gallery", "indoor"),
    ("tourism", "attraction"): ("landmark", "attraction", "unknown"),
    ("tourism", "viewpoint"): ("landmark", "viewpoint", "outdoor"),
    ("tourism", "artwork"): ("landmark", "artwork", "outdoor"),
    ("tourism", "zoo"): ("entertainment", "zoo", "outdoor"),
    ("tourism", "aquarium"): ("entertainment", "aquarium", "indoor"),
    ("tourism", "theme_park"): ("entertainment", "theme_park", "outdoor"),
    ("leisure", "park"): ("park", "park", "outdoor"),
    ("leisure", "garden"): ("park", "garden", "outdoor"),
    ("leisure", "playground"): ("kids", "playground", "outdoor"),
    ("leisure", "water_park"): ("entertainment", "water_park", "unknown"),
    ("leisure", "ice_rink"): ("sport", "ice_rink", "unknown"),
    ("leisure", "amusement_arcade"): ("entertainment", "arcade", "indoor"),
    ("leisure", "escape_game"): ("entertainment", "escape_game", "indoor"),
    ("leisure", "bowling_alley"): ("entertainment", "bowling", "indoor"),
    ("leisure", "trampoline_park"): ("kids", "trampoline_park", "indoor"),
    ("leisure", "miniature_golf"): ("entertainment", "minigolf", "unknown"),
    ("leisure", "sports_centre"): ("sport", "sports_centre", "indoor"),
    ("leisure", "stadium"): ("sport", "stadium", "mixed"),
    ("leisure", "dance"): ("entertainment", "dance", "indoor"),
    ("shop", "mall"): ("shopping", "mall", "indoor"),
    ("shop", "books"): ("shopping", "books", "indoor"),
    ("shop", "gift"): ("shopping", "souvenir", "indoor"),
    ("shop", "souvenir"): ("shopping", "souvenir", "indoor"),
    # anchors (where people are), not recommended as destinations
    ("amenity", "university"): ("anchor", "university", "indoor"),
    ("amenity", "college"): ("anchor", "college", "indoor"),
    ("amenity", "bus_station"): ("anchor", "bus_station", "indoor"),
    ("tourism", "hotel"): ("anchor", "hotel", "indoor"),
    ("railway", "station"): ("anchor", "railway_station", "indoor"),
    ("aeroway", "terminal"): ("anchor", "airport", "indoor"),
    ("aeroway", "aerodrome"): ("anchor", "airport", "indoor"),
}
KEY_PRIORITY = ("tourism", "amenity", "leisure", "shop", "aeroway", "railway")

ATTR_TAGS = (
    "cuisine", "outdoor_seating", "internet_access", "wheelchair", "smoking", "fee", "level",
    "operator", "brand", "description", "diet:vegetarian", "diet:halal", "air_conditioning",
    "changing_table", "toilets", "min_age", "max_age", "capacity", "building",
)


@dataclass
class Place:
    id: str
    name: str
    category: str
    subcategory: str
    lat: float
    lon: float
    indoor: str = "unknown"
    name_ru: str | None = None
    name_kk: str | None = None
    name_en: str | None = None
    aliases: list[str] = field(default_factory=list)
    address: str | None = None
    website: str | None = None
    phone: str | None = None
    opening_hours: str | None = None
    hours_valid: bool = False
    attrs: dict = field(default_factory=dict)
    source_id: str = "osm"
    source_url: str | None = None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def _classify(tags: dict) -> tuple[str, str, str] | None:
    for key in KEY_PRIORITY:
        val = tags.get(key)
        if val and (key, val) in CATEGORY_MAP:
            return CATEGORY_MAP[(key, val)]
    if tags.get("historic"):
        return ("landmark", tags["historic"], "outdoor")
    return None


def _indoor(default: str, tags: dict) -> str:
    if tags.get("indoor") == "yes" or tags.get("location") in ("indoor", "underground"):
        return "indoor"
    if tags.get("covered") == "yes":
        return "mixed"
    if default == "unknown" and tags.get("building") not in (None, "no"):
        return "indoor"
    return default


def _split(v: str | None) -> list[str]:
    return [x.strip() for x in v.split(";") if x.strip()] if v else []


def from_osm_element(el: dict, fetched_at: datetime | None = None) -> Place | None:
    tags = el.get("tags") or {}
    cls = _classify(tags)
    if not cls:
        return None
    category, sub, indoor = cls
    if el["type"] == "node":
        lat, lon = el.get("lat"), el.get("lon")
    else:
        c = el.get("center") or {}
        lat, lon = c.get("lat"), c.get("lon")
    if lat is None or lon is None:
        return None

    name = tags.get("name") or tags.get("name:ru") or tags.get("name:kk") or tags.get("name:en")
    if not name:
        if sub != "playground":
            return None
        name = "Детская площадка"

    aliases: list[str] = []
    for k in ("alt_name", "old_name", "short_name", "official_name", "alt_name:ru", "alt_name:kk"):
        aliases += _split(tags.get(k))

    street, house = tags.get("addr:street"), tags.get("addr:housenumber")
    address = ", ".join(x for x in (street, house) if x) or None
    oh = tags.get("opening_hours")
    attrs = {k: tags[k] for k in ATTR_TAGS if k in tags}
    if tags.get("internet_access") in ("wlan", "yes", "wifi"):
        attrs["wifi"] = True

    return Place(
        id=f"osm:{el['type']}/{el['id']}",
        name=name,
        category=category,
        subcategory=sub,
        lat=float(lat),
        lon=float(lon),
        indoor=_indoor(indoor, tags),
        name_ru=tags.get("name:ru"),
        name_kk=tags.get("name:kk"),
        name_en=tags.get("name:en"),
        aliases=sorted(set(aliases)),
        address=address,
        website=tags.get("website") or tags.get("contact:website") or tags.get("url"),
        phone=tags.get("phone") or tags.get("contact:phone"),
        opening_hours=oh,
        hours_valid=hours.is_valid(oh),
        attrs=attrs,
        source_url=f"https://www.openstreetmap.org/{el['type']}/{el['id']}",
        fetched_at=fetched_at or datetime.now(timezone.utc),
    )


def normalize_overpass(data: dict) -> list[Place]:
    fetched_raw = data.get("_fetched_at")
    fetched = datetime.fromisoformat(fetched_raw) if fetched_raw else datetime.now(timezone.utc)
    seen: set[str] = set()
    out: list[Place] = []
    for el in data.get("elements", []):
        p = from_osm_element(el, fetched)
        if p and p.id not in seen:
            seen.add(p.id)
            out.append(p)
    return out
