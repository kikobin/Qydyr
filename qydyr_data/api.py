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

Privacy: coordinates are rounded to ~100 m on arrival and used in memory for one answer; logs and the pulse
keep a ~1 km cell only (query strings are cut from the access log).
If a request carries Telegram Mini App initData, its signature is checked. Endpoints that call Claude are
rate-limited per Telegram user (valid initData) or per IP.
"""
from __future__ import annotations

import hmac
import logging
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Iterator, Literal

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from . import city, settings, store
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
from .telegram_auth import bot_key, validate_init_data

log = logging.getLogger("qydyr.api")
app = FastAPI(title="Qydyr API", version="1.0")
# the Mini App and the pulse screen are served from this same origin; other sites get no CORS
app.add_middleware(CORSMiddleware, allow_origins=[settings.PUBLIC_URL] if settings.PUBLIC_URL else ["*"],
                   allow_methods=["*"], allow_headers=["*"])

_PLACES_CACHE: tuple[float, list] | None = None
Preset = Literal["blizzard", "frost", "rain", "summer_evening", "smog"]
Lat = Annotated[float, Field(ge=-90, le=90)]
Lon = Annotated[float, Field(ge=-180, le=180)]


class _NoQuery(logging.Filter):
    """uvicorn's access log line carries the full path: drop the query (?lat=…&lon=…), keep method and route."""
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) >= 3 and isinstance(record.args[2], str):
            record.args = (*record.args[:2], record.args[2].split("?", 1)[0], *record.args[3:])
        return True


logging.getLogger("uvicorn.access").addFilter(_NoQuery())

# Claude costs money per call: a few per minute per person is plenty for a human, not for a script.
# Behind one venue Wi-Fi a whole jury shares an IP, so the IP budget is wider than the per-user one.
LIMITS = {"user": 30, "ip": 120}        # requests per minute
_HITS: dict[str, deque] = defaultdict(deque)


def _costly(request: Request) -> bool:
    p = request.url.path
    if request.method == "POST" and (p == "/ask" or p.startswith("/ask/") or p == "/whereami" or p.endswith("/ask")):
        return True
    if p.startswith("/story/") or (p.startswith("/tours/") and p.endswith("/story")):
        return True
    return request.query_params.get("lang", "ru") != "ru" and (p == "/walks" or p.startswith("/tours/"))


def _over_limit(key: str, limit: int, now: float) -> bool:
    q = _HITS[key]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= limit:
        return True
    q.append(now)
    if len(_HITS) > 20000:   # forget idle keys
        for k in [k for k, v in _HITS.items() if not v or now - v[-1] > 60]:
            del _HITS[k]
    return False


@app.middleware("http")
async def telegram_init_data(request: Request, call_next):
    """Mini App requests carry initData; when a bot token is configured, a bad signature is rejected.
    Without initData (a normal browser, the pulse screen) the app still works, under the per-IP budget."""
    init = request.headers.get("x-telegram-init-data")
    user = None
    if init and settings.TELEGRAM_BOT_TOKEN:
        fields = validate_init_data(init, settings.TELEGRAM_BOT_TOKEN)
        if fields is None:
            return JSONResponse({"detail": "invalid Telegram initData"}, status_code=401)
        user = (fields.get("user") or {}).get("id") if isinstance(fields.get("user"), dict) else None
    is_bot = bool(settings.TELEGRAM_BOT_TOKEN) and hmac.compare_digest(
        request.headers.get("x-qydyr-bot", ""), bot_key(settings.TELEGRAM_BOT_TOKEN))
    if settings.ANTHROPIC_API_KEY and _costly(request) and not is_bot:   # the bot is one trusted client
        key, limit = (f"u:{user}", LIMITS["user"]) if user else (f"ip:{request.client.host if request.client else '-'}", LIMITS["ip"])
        if _over_limit(key, limit, time.monotonic()):
            return JSONResponse({"detail": "too many requests, try again in a minute"}, status_code=429,
                                headers={"Retry-After": "60"})
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
    if at is not None and not 2000 <= at.year <= 2100:
        raise HTTPException(422, "at: a date between 2000 and 2100")
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


_NARRATOR = None


def _narrator():
    """RAG generation: the picked places' facts -> one human "why" each (assistant/narrate.py)."""
    global _NARRATOR
    if not settings.ANTHROPIC_API_KEY:
        return None
    if _NARRATOR is None:
        from .assistant.narrate import Narrator
        _NARRATOR = Narrator()
    return _NARRATOR


class AskIn(BaseModel):
    text: str = Field("", max_length=1000)
    anchor: Anchor | None = None                         # a known place (photo answers, group meeting points)
    lat: Lat | None = None
    lon: Lon | None = None
    at: datetime | None = None
    members: list[Annotated[str, Field(max_length=300)]] | None = Field(None, max_length=12)
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
    q: str = Field(max_length=500)
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


def _check_overrides(o: Overrides | None) -> None:
    if o is None:
        return
    for name in ("budget_min", "budget_max", "airport_buffer_min"):
        v = getattr(o, name)
        if v is not None and not 0 <= v <= 24 * 60:
            raise HTTPException(422, f"{name}: 0–1440 minutes")


def _run(body: AskIn, data: DbData, record: bool = True) -> dict:
    _check_overrides(body.overrides)
    if body.lat is not None and body.lon is not None:  # ~100 m is enough for walking minutes; exact GPS is not kept
        body.lat, body.lon = round(body.lat, 3), round(body.lon, 3)
    text = body.text.strip() or ("удиви меня" if body.surprise else "что интересного рядом")
    assistant = Assistant(data, llm=_llm_intent() if body.llm else None, places=_places(data))
    ans = assistant.ask(text, at=_local(body.at), lat=body.lat, lon=body.lon, members=body.members,
                        overrides=body.overrides, weather_preset=body.weather_preset, surprise=body.surprise,
                        seed=body.seed, anchor=body.anchor.model_copy() if body.anchor else None, **_demo_kw())
    if ans.anchor is not None and record:   # a chip edit re-runs the same request: it is not a new person asking
        needs, unmet = city.unmet_needs(ans.intent, ans.items)
        PULSE.record(ans.anchor.lat, ans.anchor.lon, list(ans.intent.needs), ans.intent.lang, len(ans.items),
                     body.surprise, unmet=unmet)
        cell = geohash(ans.anchor.lat, ans.anchor.lon, city.CELL_PRECISION)
        log.info("ask cell=%s items=%d", cell, len(ans.items))
        if needs:
            try:   # counts for the city page; the person's answer never waits on or fails because of it
                store.record_demand(data.conn, cell, needs, unmet, ans.at.date())
            except Exception as e:
                log.warning("demand not recorded: %s", e)
                data.conn.rollback()
    lang = body.lang or ans.intent.lang
    narrator = _narrator() if body.llm else None
    out: dict[str, Any] = {"lang": "ru", "translated": False}
    if body.format == "structured" and lang != "ru":
        # the Mini App and the bot: Claude writes the "why" straight in the person's language while the rest
        # of the answer (statuses, chips, reasons) is translated next to it, not one after the other
        dump = ans.model_dump(mode="json")
        before = [it.what_to_do for it in ans.items]
        with ThreadPoolExecutor(max_workers=1) as pool:
            job = pool.submit(translate_answer, dump, lang)
            if narrator is not None:
                narrator(ans, assistant.picked, lang=lang)
            out["translated"] = job.result()
        for d, it, was in zip(dump["items"], ans.items, before):
            d["pitch"] = it.pitch
            if it.what_to_do != was:
                d["what_to_do"] = it.what_to_do
        dump["narrated"], dump["used_data"] = ans.narrated, list(ans.used_data)
        out["answer"] = dump
        if out["translated"] or ans.narrated:
            out["lang"] = lang
        _remember(body, ans, dump)
        return out
    if narrator is not None:
        narrator(ans, assistant.picked)
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
    _remember(body, ans, dump)
    return out


def _remember(body: AskIn, ans, dump: dict) -> None:
    req = body.model_dump(mode="json")
    if ans.anchor is not None and ans.anchor.source == "gps":  # keep only the coarse cell, never exact GPS
        req["lat"], req["lon"] = ans.anchor.lat, ans.anchor.lon
    ANSWERS.put(ans.id, {"request": req, "answer": dump})


@app.post("/ask")
def ask(body: AskIn, data: DbData = Depends(get_data)) -> dict:
    return _run(body, data)


@app.post("/ask/{answer_id}/patch")
def ask_patch(answer_id: str, body: PatchIn, data: DbData = Depends(get_data)) -> dict:
    prev = ANSWERS.get(answer_id)
    if prev is None:
        raise HTTPException(404, "answer expired")
    req = AskIn.model_validate(prev["request"])
    try:
        req.overrides = (req.overrides or Overrides()).merged(body.patch)
    except (ValidationError, TypeError) as e:
        raise HTTPException(422, f"bad patch: {e.__class__.__name__}")
    req.format = body.format
    return _run(req, data, record=False)


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
def walks(lat: Lat, lon: Lon, at: datetime | None = None, weather_preset: Preset | None = None,
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
    return Claude(settings.CLAUDE_MODEL, fast=True)


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
def weather_now(lat: Lat, lon: Lon, at: datetime | None = None, weather_preset: Preset | None = None) -> dict:
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
    lat: Lat
    lon: Lon
    visited: list[str] = Field(default_factory=list, max_length=100)
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
def walk_nudges(lat: Lat, lon: Lon, at: datetime | None = None, next_lat: Lat | None = None,
                next_lon: Lon | None = None, weather_preset: Preset | None = None,
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
        llm = Claude(settings.CLAUDE_MODEL, fast=True)
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
        llm = Claude(settings.CLAUDE_MODEL, fast=True)
    return tell(row, evs, lang, llm=llm)


@app.get("/qr.svg", include_in_schema=False)
def qr_svg(data: str = Query(..., max_length=500), dark: str = Query("#0A1424", pattern=r"^#[0-9A-Fa-f]{3,8}$"),
           light: str = Query("#EDF2F7", pattern=r"^#[0-9A-Fa-f]{3,8}$")) -> Response:
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


# ------------------------------------------------------------------ Qydyr for the city (/app/city.html)
@app.get("/city/scenarios")
def city_scenarios() -> list[dict]:
    return city.scenarios_view()


@app.get("/city/gaps")
def city_gaps(scenario: str = Query("kids_evening", pattern="^[a-z_]{1,32}$"), data: DbData = Depends(get_data)) -> dict:
    """Where the city cannot answer a typical request: the supply audit, cell by cell (~1 km)."""
    if scenario not in city.BY_ID:
        raise HTTPException(404, "no such scenario")
    return city.gaps_view(data.conn, scenario)


@app.get("/city/demand")
def city_demand(data: DbData = Depends(get_data)) -> dict:
    """What people looked for and did not find: counts per ~1 km cell and need, shown from DEMAND_K requests."""
    return city.demand_view(data.conn, live=PULSE.unmet_live())


@app.get("/city/data")
def city_data(data: DbData = Depends(get_data)) -> dict:
    """Where every answer comes from: sources, freshness, coverage."""
    return city.data_view(data.conn)


@app.get("/whats-on")
def whats_on(lat: Lat, lon: Lon, radius: float = Query(1500, gt=0, le=20000), at: datetime | None = None,
             horizon: int = Query(180, ge=0, le=7 * 24 * 60),
             data: DbData = Depends(get_data)) -> list[dict]:
    rows = store.whats_on(data.conn, lat, lon, radius, _local(at), horizon)
    return [{**r, "badge": describe_event(r)} for r in rows]


@app.get("/places-near")
def places_near(lat: Lat, lon: Lon, radius: float = Query(800, gt=0, le=5000), at: datetime | None = None,
                categories: str | None = Query(default=None, description="food,culture,park,..."),
                limit: int = Query(50, ge=1, le=200), data: DbData = Depends(get_data)) -> list[dict]:
    cats = categories.split(",") if categories else None
    rows = store.places_near(data.conn, lat, lon, radius, _local(at), cats, limit)
    return [{**r, "badge": describe_place(r)} for r in rows]


if settings.WEB_DIR.exists():
    app.mount("/app", StaticFiles(directory=settings.WEB_DIR, html=True), name="app")

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/app/")
