"""Astana City MCP: the same city knowledge as Qydyr, as tools any MCP client (Claude Desktop, IDEs,
agents) can call — "what is on near the National Museum tonight?", "is this café open and does it
have Wi-Fi?". Answers come from the database (OpenStreetMap + afishas), never from model memory.

    python -m qydyr_data mcp                         # stdio, for Claude Desktop
    python -m qydyr_data mcp --transport streamable-http

Claude Desktop (claude_desktop_config.json):
    {"mcpServers": {"astana-city": {"command": "python", "args": ["-m", "qydyr_data", "mcp"],
                                    "env": {"DATABASE_URL": "postgresql://..."}}}}
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import settings, store
from .assistant import placeqa
from .assistant.recommend import Assistant, DbData
from .assistant.render import render
from .badges import describe_event, describe_place

server = MCPServer(
    name="astana-city",
    title="Astana City (Qydyr)",
    version="1.0",
    description="Live city knowledge for Astana: places with opening hours, events happening now, "
                "a walking assistant that picks 1–3 options for the time you have, and guided walks with sourced facts.",
    instructions=("Use these tools for anything about going out in Astana (Kazakhstan) right now. Coordinates are "
                  "WGS84. Every result carries its source; say 'unknown' when a field is unknown instead of guessing."),
)


def _json(x: Any) -> Any:
    return json.loads(json.dumps(x, default=lambda o: o.isoformat() if isinstance(o, datetime) else str(o)))


def _at(at: str | None) -> datetime | None:
    if not at:
        return None
    d = datetime.fromisoformat(at)
    return d if d.tzinfo else d.replace(tzinfo=settings.TZ)


@server.tool(description="Ask the walking assistant like a person would, e.g. 'I'm at Khan Shatyr, 1 hour, quiet "
                         "coffee with a socket'. Returns 1–3 picks with why, how long, what to do, and sources.")
def ask_astana(request: str, lat: float | None = None, lon: float | None = None, at: str | None = None) -> dict:
    with store.connect() as conn:
        ans = Assistant(DbData(conn)).ask(request, lat=lat, lon=lon, at=_at(at))
    items = [{"title": i.title, "status": i.status, "walk_min": i.walk_min, "distance_m": i.distance_m,
              "visit_min": list(i.visit_min), "why": i.why, "what_to_do": i.what_to_do,
              "what_to_do_source": "listing" if i.what_to_do_source == "data" else "assistant idea",
              "warnings": i.warnings, "sources": i.sources, "lat": i.lat, "lon": i.lon, "confidence": i.confidence}
             for i in ans.items]
    return _json({"understood": [c.label for c in ans.chips if c.active], "items": items, "notes": ans.notes,
                  "why_not": [{"title": r.title, "reason": r.reason} for r in ans.rejected],
                  "text": render(ans)})


@server.tool(description="Events happening now or soon near a point: evening exhibitions, temporary shows, concerts. "
                         "Each has a state (now/soon/later_today) and a human badge like 'Идёт сейчас · до 22:00'.")
def whats_on(lat: float, lon: float, radius_m: float = 1500, at: str | None = None, horizon_min: int = 180) -> list[dict]:
    with store.connect() as conn:
        rows = store.whats_on(conn, lat, lon, radius_m, _at(at), horizon_min)
    return _json([{"title": r["title"], "venue": r.get("venue"), "state": r["state"], "badge": describe_event(r),
                   "distance_m": r["distance_m"], "is_free": r.get("is_free"), "url": r.get("url"),
                   "confidence": r.get("confidence")} for r in rows])


@server.tool(description="Places near a point with their open state at the given time (open until / opens at / "
                         "hours unknown). Optional categories: food, culture, park, landmark, nightlife, shopping, kids.")
def places_near(lat: float, lon: float, radius_m: float = 800, categories: list[str] | None = None,
                at: str | None = None, limit: int = 30) -> list[dict]:
    with store.connect() as conn:
        rows = store.places_near(conn, lat, lon, radius_m, _at(at), categories, limit)
    return _json([{"place_id": r["place_id"], "name": r["name"], "category": r["category"],
                   "subcategory": r["subcategory"], "state": describe_place(r), "distance_m": r["distance_m"],
                   "opening_hours": r.get("opening_hours"), "source": r.get("source_url")} for r in rows])


@server.tool(description="A question about one place ('до скольки работает?', 'есть розетки?', 'можно с ребёнком?'), "
                         "answered only from the data: yes / likely / unknown / no, with evidence.")
def place_question(place_id: str, question: str, at: str | None = None) -> dict:
    when = _at(at) or datetime.now(settings.TZ)
    with store.connect() as conn:
        row = store.place(conn, place_id, when)
        if row is None:
            return {"answer": "no such place", "certainty": "unknown", "evidence": []}
        evs = [e for e in store.whats_on(conn, row["lat"], row["lon"], 150, when, 24 * 60) if e.get("place_id") == place_id]
    return placeqa.answer(row, evs, question, when).model_dump()


@server.tool(description="Guided walks around Astana with sourced facts (Independence Square, EXPO, ...). Without "
                         "tour_id: the list. With tour_id: its stops in order with coordinates, facts (each with a "
                         "source URL) and a tip for what to do at each stop. lang: ru, kk or en.")
def guided_walks(tour_id: str | None = None, lang: str = "ru") -> dict:
    from .assistant import tours as TOURS
    lang = lang if lang in ("ru", "kk", "en") else "ru"
    if not tour_id:
        return {"tours": TOURS.tour_cards(lang)}
    t = TOURS.get_tour(tour_id)
    if t is None:
        return {"error": f"no tour {tour_id}", "tours": [c["id"] for c in TOURS.tour_cards(lang)]}
    view = TOURS.tour_view(t, lang)
    return _json({k: view[k] for k in ("id", "title", "subtitle", "meta", "intro", "hours", "stops_list", "legs")})


def main(transport: str = "stdio") -> None:
    server.run(transport=transport)  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
