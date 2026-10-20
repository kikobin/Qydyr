"""Astana City MCP tools on the PostGIS fixtures."""
import asyncio
import json

from qydyr_data import settings
from qydyr_data.mcp_server import server

from .conftest import TEST_DSN
from .test_pipeline_db import load_everything

AT = "2026-10-23T19:10:00"


def call(name, args):
    """The tool's JSON, whatever envelope this SDK version wraps it in."""
    r = asyncio.run(server.call_tool(name, args))
    if isinstance(r, tuple):
        r = r[1] if r[1] is not None else r[0]
    if getattr(r, "structured_content", None):
        r = r.structured_content
    elif hasattr(r, "content"):
        r = r.content
    if isinstance(r, list) and r and hasattr(r[0], "text"):
        parsed = [json.loads(c.text) for c in r]
        r = parsed[0] if len(parsed) == 1 and isinstance(parsed[0], dict) else parsed
    if isinstance(r, dict) and set(r) == {"result"}:
        r = r["result"]
    return r


def test_mcp_tools(db, sample_places, monkeypatch):
    load_everything(db, sample_places)
    monkeypatch.setattr(settings, "DATABASE_URL", TEST_DSN)
    tools = {t.name for t in asyncio.run(server.list_tools())}
    assert {"ask_astana", "whats_on", "places_near", "place_question", "guided_walks"} <= tools
    from qydyr_data.assistant import tours as TOURS
    monkeypatch.setattr(TOURS, "TOURS_FILE", settings.ROOT / "tests" / "fixtures" / "tours_sample.yaml")
    walks = call("guided_walks", {"lang": "en"})["tours"]
    assert walks[0]["id"] == "test_expo" and walks[0]["title"] == "Test walk"
    walk = call("guided_walks", {"tour_id": "test_expo"})
    assert walk["stops_list"][0]["facts"][0]["source"].startswith("https://") and walk["intro"].startswith("Я Суырбек")
    on = call("whats_on", {"lat": 51.1186, "lon": 71.4701, "at": AT})
    assert on[0]["badge"].startswith("Идёт сейчас") and "Ночь в музее" in on[0]["title"]
    near = call("places_near", {"lat": 51.1186, "lon": 71.4701, "radius_m": 300, "categories": ["food"], "at": AT})
    assert near[0]["state"] == "открыто до 23:00"
    ans = call("ask_astana", {"request": "Хочу тихое место с кофе и розеткой", "lat": 51.09, "lon": 71.4165, "at": "2026-10-23T15:00:00"})
    assert ans["items"] and ans["items"][0]["title"] and "Понял так" in ans["text"]
    qa = call("place_question", {"place_id": near[0]["place_id"], "question": "до скольки работает?", "at": AT})
    assert qa["certainty"] == "yes" and "23:00" in qa["answer"]
