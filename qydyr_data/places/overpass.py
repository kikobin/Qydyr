"""Collect Astana points of interest from OpenStreetMap via the Overpass API.

OSM data is © OpenStreetMap contributors, ODbL. Show the attribution wherever places are displayed.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .. import settings

log = logging.getLogger(__name__)

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# (key, regex of values). Only named objects, except playgrounds (they matter for "with a child"
# and are usually unnamed in courtyards).
NAMED_FILTERS: list[tuple[str, str]] = [
    ("amenity", "cafe|restaurant|fast_food|food_court|ice_cream|bar|pub|nightclub|cinema|theatre|"
                "arts_centre|library|planetarium|community_centre|coworking_space|marketplace|fountain|"
                "concert_hall|events_venue|exhibition_centre|place_of_worship|hookah_lounge|karaoke_box|biergarten"),
    ("tourism", "museum|gallery|attraction|viewpoint|artwork|zoo|aquarium|theme_park"),
    ("leisure", "park|garden|water_park|ice_rink|amusement_arcade|escape_game|bowling_alley|"
                "trampoline_park|miniature_golf|sports_centre|stadium|dance"),
    ("shop", "mall|books|gift|souvenir"),
    # anchors: not destinations, but how people say where they are ("я в AITU", "2 часа до вылета")
    ("amenity", "university|college|bus_station"),
    ("tourism", "hotel"),
    ("railway", "station"),
    ("aeroway", "terminal|aerodrome"),
]


def build_query(bbox: tuple[float, float, float, float] = settings.ASTANA_BBOX, timeout_s: int = 180) -> str:
    s, w, n, e = bbox
    b = f"({s},{w},{n},{e})"
    lines = [f'  nwr["{k}"~"^({v})$"]["name"]{b};' for k, v in NAMED_FILTERS]
    lines.append(f'  nwr["historic"]["name"]{b};')
    lines.append(f'  nwr["leisure"="playground"]{b};')
    body = "\n".join(lines)
    return f"[out:json][timeout:{timeout_s}];\n(\n{body}\n);\nout center tags;"


def fetch(bbox=settings.ASTANA_BBOX, save_raw_to: Path | None = None) -> dict:
    query = build_query(bbox)
    headers = {"User-Agent": settings.USER_AGENT}
    last_err: Exception | None = None
    for url in OVERPASS_ENDPOINTS:
        try:
            log.info("Overpass request to %s", url)
            r = httpx.post(url, data={"data": query}, headers=headers, timeout=240)
            r.raise_for_status()
            data = r.json()
            data["_fetched_at"] = datetime.now(timezone.utc).isoformat()
            data["_endpoint"] = url
            if save_raw_to:
                save_raw_to.parent.mkdir(parents=True, exist_ok=True)
                save_raw_to.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            log.info("Overpass returned %d elements", len(data.get("elements", [])))
            return data
        except (httpx.HTTPError, ValueError) as e:
            log.warning("Overpass endpoint %s failed: %s", url, e)
            last_err = e
    raise RuntimeError(f"all Overpass endpoints failed: {last_err}")
