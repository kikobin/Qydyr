"""HTTP API for the Telegram bot, the Mini App and the 'Пульс Астаны' screen.

    uvicorn qydyr_data.api:app --reload
    POST /ask                 {"text", "lat?", "lon?", "at?", "overrides?", "weather_preset?", "surprise?"}
    POST /ask/{id}/patch      {"patch": {...}}  — re-run a previous answer with one chip changed
    GET  /answer/{id}         a recent answer
    POST /feedback            {"answer_id", "vote": "up"|"down"}
    GET  /walks               ?lat&lon&at&weather_preset&lang   — themed walks assembled for right now
    GET  /tours, /tours/{id}  guided walks with sourced facts; POST /tours/{id}/progress (live location)
    GET  /tours/{id}/stops/{sid}/story, /walk/nudges, /weather   — the walk mode
    GET  /place/{id}          place card: open state, events inside
    POST /place/{id}/ask      {"q"}  — "есть ли тут розетки?" answered from facts only
    GET  /pulse               anonymous live stats for the big screen
    GET  /whats-on, /places-near, /config, /health
    /app/                     the Mini App (also works in a normal browser), /app/pulse.html — the jury screen

Privacy: coordinates are used in memory for one answer; logs and the pulse keep a ~1 km cell only.
If a request carries Telegram Mini App initData, its signature is checked.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Literal

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import settings, store
from .assistant import placeqa
from .assistant import tours as TOURS
from .assistant.mascot import weather_view
from .assistant.models import Anchor, Overrides
from .assistant.privacy import geohash
from .assistant.recommend import Assistant, DbData
from .assistant.render import render, render_why_not
from .assistant.translate import translate, translate_answer
from .assistant.walks import build_walks
from .assistant.weather import PRESETS
from .badges import describe_event, describe_place
from .pulse import ANSWERS, PULSE
from .telegram_auth import validate_init_data

log = logging.getLogger("qydyr.api")
app = FastAPI(title="Qydyr API", version="1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_PLACES_CACHE: tuple[float, list] | None = None
Preset = Literal["blizzard", "frost", "rain", "summer_evening", "smog"]


@app.middleware("http")
async def telegram_init_data(request: Request, call_next):
    """Mini App requests carry initData; when a bot token is configured, a bad signature is rejected."""
    init = request.headers.get("x-telegram-init-data")
    if init and settings.TELEGRAM_BOT_TOKEN and validate_init_data(init, settings.TELEGRAM_BOT_TOKEN) is None:
        return JSONResponse({"detail": "invalid Telegram initData"}, status_code=401)
    return await call_next(request)


def get_data() -> Iterator[DbData]:
    with store.connect() as conn:
        yield DbData(conn)


def _places(data: DbData) -> list:
    global _PLACES_CACHE
    if _PLACES_CACHE is None or time.time() - _PLACES_CACHE[0] > 600:
        _PLACES_CACHE = (time.time(), data.places_lite())
    return _PLACES_CACHE[1]


def _local(at: datetime | None) -> datetime | None:
    if at is None:
        if settings.DEMO_NOW:  # demo mode: a frozen "now"
            return datetime.fromisoformat(settings.DEMO_NOW).replace(tzinfo=settings.TZ)
        return None
    return at if at.tzinfo else at.replace(tzinfo=settings.TZ)


def _now() -> datetime:
    return _local(None) or datetime.now(settings.TZ)


def _demo_kw() -> dict:
    kw: dict[str, Any] = {}
    if settings.WEATHER_FILE:
        kw["weather_file"] = Path(settings.WEATHER_FILE)
    if settings.AIR_FILE:
        kw["air_file"] = Path(settings.AIR_FILE)
    return kw


def _llm_intent():
    if not settings.ANTHROPIC_API_KEY:
        return None
    from .assistant.intent import ClaudeIntentParser
    return ClaudeIntentParser()


class AskIn(BaseModel):
    text: str = ""
    anchor: Anchor | None = None                         # a known place (photo answers, group meeting points)
    lat: float | None = None
    lon: float | None = None
    at: datetime | None = None
    members: list[str] | None = None
    overrides: Overrides | None = None
    weather_preset: Preset | None = None
    surprise: bool = False
    seed: int | None = None
    lang: Literal["ru", "kk", "en"] | None = None        # language of the interface / the answer
    format: Literal["full", "structured", "text"] = "full"
    llm: bool = True                                     # let Claude help understand the request


class PatchIn(BaseModel):
    patch: dict[str, Any] = Field(default_factory=dict)
    format: Literal["full", "structured", "text"] = "full"


class FeedbackIn(BaseModel):
    answer_id: str
    vote: Literal["up", "down"]
    item: int | None = None


class QAIn(BaseModel):
    q: str
    at: datetime | None = None


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/config")
def config() -> dict:
    return {
        "version": app.version,
        "now": _now().isoformat(),
        "map_style": {"dark": settings.MAP_STYLE_DARK, "light": settings.MAP_STYLE_LIGHT},
        "bot_username": settings.BOT_USERNAME,
        "llm": bool(settings.ANTHROPIC_API_KEY),
        "presets": [{"id": "now", "label": "сейчас"}] + [{"id": k, "label": v["label"]} for k, v in PRESETS.items()],
        "attribution": ["© OpenStreetMap contributors", "Weather: Open-Meteo", "Air: Open-Meteo, CAMS"],
    }


def _run(body: AskIn, data: DbData) -> dict:
    text = body.text.strip() or ("удиви меня" if body.surprise else "что интересного рядом")
    assistant = Assistant(data, llm=_llm_intent() if body.llm else None, places=_places(data))
    ans = assistant.ask(text, at=_local(body.at), lat=body.lat, lon=body.lon, members=body.members,
                        overrides=body.overrides, weather_preset=body.weather_preset, surprise=body.surprise,
                        seed=body.seed, anchor=body.anchor.model_copy() if body.anchor else None, **_demo_kw())
    if ans.anchor is not None:
        PULSE.record(ans.anchor.lat, ans.anchor.lon, list(ans.intent.needs), ans.intent.lang, len(ans.items),
                     body.surprise)
        log.info("ask cell=%s items=%d", geohash(ans.anchor.lat, ans.anchor.lon, 6), len(ans.items))
    lang = body.lang or ans.intent.lang
    out: dict[str, Any] = {"lang": "ru", "translated": False}
    dump = ans.model_dump(mode="json")
    if body.format in ("full", "text"):
        t = render(ans)
        tr = translate(t, lang) if lang != "ru" else None
        out["text"] = tr or t
        out["why_not_text"] = render_why_not(ans)
    if body.format in ("full", "structured"):
        if lang != "ru" and translate_answer(dump, lang):
            out["translated"] = True
        out["answer"] = dump
    if lang != "ru" and (out["translated"] or "text" in out and out["text"] != render(ans)):
        out["lang"] = lang
    req = body.model_dump(mode="json")
    if ans.anchor is not None and ans.anchor.source == "gps":  # keep only the coarse cell, never exact GPS
        req["lat"], req["lon"] = ans.anchor.lat, ans.anchor.lon
    ANSWERS.put(ans.id, {"request": req, "answer": dump})
    return out


@app.post("/ask")
def ask(body: AskIn, data: DbData = Depends(get_data)) -> dict:
    return _run(body, data)


@app.post("/ask/{answer_id}/patch")
def ask_patch(answer_id: str, body: PatchIn, data: DbData = Depends(get_data)) -> dict:
    prev = ANSWERS.get(answer_id)
    if prev is None:
        raise HTTPException(404, "answer expired")
    req = AskIn.model_validate(prev["request"])
    req.overrides = (req.overrides or Overrides()).merged(body.patch)
    req.format = body.format
    return _run(req, data)


@app.get("/answer/{answer_id}")
def get_answer(answer_id: str) -> dict:
    prev = ANSWERS.get(answer_id)
    if prev is None:
        raise HTTPException(404, "answer expired")
    return prev["answer"]


@app.post("/feedback")
def feedback(body: FeedbackIn) -> dict:
    PULSE.vote(body.vote == "up")
    log.info("feedback %s item=%s", body.vote, body.item)
    return {"ok": True}


@app.get("/walks")
def walks(lat: float, lon: float, at: datetime | None = None, weather_preset: Preset | None = None,
          lang: Literal["ru", "kk", "en"] = "ru", data: DbData = Depends(get_data)) -> list[dict]:
    assistant = Assistant(data, places=_places(data))
    anchor = Anchor(lat=lat, lon=lon, label="ваша геолокация", source="gps")
    out = build_walks(assistant, anchor, _local(at) or _now(), lang=lang, weather_preset=weather_preset,
                      **_demo_kw())
    if lang != "ru" and settings.ANTHROPIC_API_KEY and out:   # the cards inside each walk, when Claude is on
        with ThreadPoolExecutor(max_workers=min(5, len(out))) as pool:
            list(pool.map(lambda w: translate_answer(w["answer"], lang), out))
    for w in out:   # every generated walk can be walked with the guide, like a curated tour
        w["tour"] = TOURS.tour_from_answer(w, lang)
    return out


# ------------------------------------------------------------------ guided walks (curated tours)
def _llm_text():
    if not settings.ANTHROPIC_API_KEY:
        return None
    from .llm import Claude
    return Claude(settings.CLAUDE_MODEL)


def _weather_at(lat: float, lon: float, at: datetime, preset_name: str | None = None):
    from .assistant import air as A
    from .assistant import weather as W
    wf = Path(settings.WEATHER_FILE) if settings.WEATHER_FILE else None
    af = Path(settings.AIR_FILE) if settings.AIR_FILE else None
    w = W.get(lat, lon, at, wf)
    a = A.get(lat, lon, at, af) if af or (w is not None and wf is None) else None
    if w is not None and a is not None:
        w.air = a
    if preset_name:
        w = W.preset(preset_name, at, real=w, lat=lat, lon=lon)
    return w


@app.get("/weather")
def weather_now(lat: float, lon: float, at: datetime | None = None, weather_preset: Preset | None = None) -> dict:
    """Weather for the walk screen and the mascot's outfit (cold, smog, rain, night...)."""
    at_ = _local(at) or _now()
    view = weather_view(_weather_at(round(lat, 2), round(lon, 2), at_, weather_preset), at_)
    if view is None:
        raise HTTPException(503, "weather unavailable")
    return view


@app.get("/tours")
def tours_list(lang: Literal["ru", "kk", "en"] = "ru") -> list[dict]:
    """Прогулки с гидом: hand-made routes with sourced facts."""
    return TOURS.tour_cards(lang)


@app.get("/tours/{tour_id}")
def tour_get(tour_id: str, lang: Literal["ru", "kk", "en"] = "ru") -> dict:
    t = TOURS.get_tour(tour_id)
    if t is None:
        raise HTTPException(404, "no such tour")
    return TOURS.tour_view(t, lang, llm=_llm_text() if lang != "ru" else None)


class ProgressIn(BaseModel):
    lat: float
    lon: float
    visited: list[str] = Field(default_factory=list)
    accuracy: float | None = None


@app.post("/tours/{tour_id}/progress")
def tour_progress(tour_id: str, body: ProgressIn) -> dict:
    """One location update during a walk (the bot's live location). Used in memory, never logged.
    The Mini App runs the same rules on the phone and does not send its position here."""
    t = TOURS.get_tour(tour_id)
    if t is None:
        raise HTTPException(404, "no such tour")
    return TOURS.progress(t, body.lat, body.lon, body.visited, body.accuracy)


@app.get("/tours/{tour_id}/stops/{stop_id}/story")
def tour_story(tour_id: str, stop_id: str, lang: Literal["ru", "kk", "en"] = "ru") -> dict:
    t = TOURS.get_tour(tour_id)
    s = t.stop(stop_id) if t else None
    if s is None:
        raise HTTPException(404, "no such stop")
    return TOURS.stop_story(t, s, lang, llm=_llm_text())


@app.get("/walk/nudges")
def walk_nudges(lat: float, lon: float, at: datetime | None = None, next_lat: float | None = None,
                next_lon: float | None = None, weather_preset: Preset | None = None,
                lang: Literal["ru", "kk", "en"] = "ru", data: DbData = Depends(get_data)) -> dict:
    """Hints on the way: events starting next to you, a place to warm up, bad air. A ~100 m cell is
    enough for "nearby", so the position is rounded before use."""
    from .assistant.travel import walk_min
    at_ = _local(at) or _now()
    lat, lon = round(lat, 3), round(lon, 3)
    w = _weather_at(round(lat, 2), round(lon, 2), at_, weather_preset)
    events = store.whats_on(data.conn, lat, lon, 450, at_, 45)
    leg = walk_min(lat, lon, next_lat, next_lon) if next_lat is not None and next_lon is not None else None
    warm = []
    if w is not None and leg and leg > w.max_outdoor_min:
        rows = store.places_near(data.conn, lat, lon, 450, at_, ["food", "shopping", "culture"], 20)
        warm = sorted([r for r in rows if r.get("indoor") == "indoor" and r.get("open_state") == "open"],
                      key=lambda r: r["distance_m"])
    return {"nudges": TOURS.nudges(events, weather=w, next_leg_min=leg, warm=warm, lang=lang),
            "weather": weather_view(w, at_)}


@app.get("/place/{place_id:path}/events")
def place_events(place_id: str, at: datetime | None = None, data: DbData = Depends(get_data)) -> list[dict]:
    row = store.place(data.conn, place_id, _local(at))
    if row is None:
        raise HTTPException(404, "no such place")
    evs = [e for e in store.whats_on(data.conn, row["lat"], row["lon"], 150, _local(at), 24 * 60)
           if e.get("place_id") == place_id]
    return [{**e, "badge": describe_event(e)} for e in evs]


@app.get("/place/{place_id:path}")
def place(place_id: str, at: datetime | None = None, data: DbData = Depends(get_data)) -> dict:
    row = store.place(data.conn, place_id, _local(at))
    if row is None:
        raise HTTPException(404, "no such place")
    return {**row, "badge": describe_place(row), "events": place_events(place_id, at, data)}


@app.post("/place/{place_id:path}/ask")
def place_ask(place_id: str, body: QAIn, data: DbData = Depends(get_data)) -> dict:
    at = _local(body.at) or _now()
    row = store.place(data.conn, place_id, at)
    if row is None:
        raise HTTPException(404, "no such place")
    evs = [e for e in store.whats_on(data.conn, row["lat"], row["lon"], 150, at, 24 * 60)
           if e.get("place_id") == place_id]
    llm = None
    if settings.ANTHROPIC_API_KEY:
        from .llm import Claude
        llm = Claude(settings.CLAUDE_MODEL)
    return placeqa.answer(row, evs, body.q, at, llm=llm).model_dump()


def run_whereami(image: bytes, data: DbData, lang: str = "ru", text: str = "", llm=None) -> dict:
    """Photo -> landmark -> answer from there. Shared by the API and the bot."""
    from .assistant.whereami import clean_image, identify, locate
    if llm is None:
        if not settings.ANTHROPIC_API_KEY:
            raise HTTPException(503, "photo recognition needs ANTHROPIC_API_KEY")
        from .llm import Claude
        llm = Claude(settings.CLAUDE_MODEL_SMART)
    try:
        lm = identify(clean_image(image), llm)
    except HTTPException:
        raise
    except Exception as e:
        log.warning("whereami failed: %s", e)
        raise HTTPException(422, "could not read the photo")
    assistant = Assistant(data, places=_places(data))
    place = locate(lm, assistant.locator)
    if place is None:
        return {"found": False, "landmark": lm.model_dump()}
    name = {"ru": place.name_ru, "kk": place.name_kk, "en": place.name_en}.get(lang) or place.name
    anchor = Anchor(lat=place.lat, lon=place.lon, label=name, source="photo", place_id=place.id)
    ans = assistant.ask(text.strip() or "что интересного рядом", at=_local(None), anchor=anchor, **_demo_kw())
    PULSE.record(anchor.lat, anchor.lon, list(ans.intent.needs), ans.intent.lang, len(ans.items))
    dump = ans.model_dump(mode="json")
    ANSWERS.put(ans.id, {"request": {"text": text or "что интересного рядом", "lat": None, "lon": None,
                                     "lang": lang, "anchor": anchor.model_dump()}, "answer": dump})
    return {"found": True, "landmark": lm.model_dump(), "place": name, "answer": dump, "text": render(ans)}


@app.post("/whereami")
async def whereami(photo: UploadFile = File(...), lang: str = Form("ru"), text: str = Form(""),
                   data: DbData = Depends(get_data)) -> dict:
    image = await photo.read()
    if len(image) > 12 * 1024 * 1024:
        raise HTTPException(413, "photo too large")
    return run_whereami(image, data, lang=lang, text=text)


@app.get("/story/{place_id:path}")
def place_story(place_id: str, lang: Literal["ru", "kk", "en"] = "ru", at: datetime | None = None,
                data: DbData = Depends(get_data)) -> dict:
    from .assistant.story import tell
    at_ = _local(at) or _now()
    row = store.place(data.conn, place_id, at_)
    if row is None:
        raise HTTPException(404, "no such place")
    evs = [e for e in store.whats_on(data.conn, row["lat"], row["lon"], 150, at_, 180) if e.get("place_id") == place_id]
    llm = None
    if settings.ANTHROPIC_API_KEY:
        from .llm import Claude
        llm = Claude(settings.CLAUDE_MODEL)
    return tell(row, evs, lang, llm=llm)


@app.get("/qr.svg", include_in_schema=False)
def qr_svg(data: str = Query(..., max_length=500), dark: str = "#0A1424", light: str = "#EDF2F7") -> Response:
    import io

    import segno
    buf = io.BytesIO()
    segno.make(data, error="m").save(buf, kind="svg", scale=8, border=2, dark=dark, light=light)
    return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/pulse")
def pulse(at: datetime | None = None, data: DbData = Depends(get_data)) -> dict:
    snap = PULSE.snapshot()
    snap["counters"] = store.city_counters(data.conn, _local(at))
    center = (51.1282, 71.4304)
    now_on = store.whats_on(data.conn, center[0], center[1], 15000, _local(at), 120)
    snap["now_on"] = [{"title": e["title"], "venue": e.get("venue"), "badge": describe_event(e),
                       "lat": e["lat"], "lon": e["lon"], "state": e["state"]} for e in now_on[:12]]
    return snap


@app.get("/whats-on")
def whats_on(lat: float, lon: float, radius: float = 1500, at: datetime | None = None, horizon: int = 180,
             data: DbData = Depends(get_data)) -> list[dict]:
    rows = store.whats_on(data.conn, lat, lon, radius, _local(at), horizon)
    return [{**r, "badge": describe_event(r)} for r in rows]


@app.get("/places-near")
def places_near(lat: float, lon: float, radius: float = 800, at: datetime | None = None,
                categories: str | None = Query(default=None, description="food,culture,park,..."),
                limit: int = 50, data: DbData = Depends(get_data)) -> list[dict]:
    cats = categories.split(",") if categories else None
    rows = store.places_near(data.conn, lat, lon, radius, _local(at), cats, limit)
    return [{**r, "badge": describe_place(r)} for r in rows]


if settings.WEB_DIR.exists():
    app.mount("/app", StaticFiles(directory=settings.WEB_DIR, html=True), name="app")

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/app/")
