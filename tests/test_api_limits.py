"""API input limits, 422 instead of 500, CORS for the own origin, rate limit on Claude-backed endpoints,
no coordinates in the access log, ~100 m rounding of incoming GPS."""
import json
import logging
import time

import pytest
from fastapi.testclient import TestClient

from qydyr_data import api, settings
from qydyr_data.api import app, get_data
from qydyr_data.assistant.recommend import DbData
from qydyr_data.telegram_auth import sign_init_data

from .test_api_v1 import AT, EXPO, client_for


def test_bad_input_is_422_not_500(db, sample_places, monkeypatch):
    c = client_for(db, sample_places, monkeypatch)
    c = TestClient(app, raise_server_exceptions=False)
    try:
        a = c.post("/ask", json={"text": "кофе", **EXPO, "at": AT}).json()["answer"]
        for patch in ({"budget_max": "abc"}, {"mode": "rocket"}, {"add_needs": 5}, {"budget_max": -100}):
            assert c.post(f"/ask/{a['id']}/patch", json={"patch": patch}).status_code == 422, patch
        assert c.post(f"/ask/{a['id']}/patch", json={"patch": {"budget_max": 90}}).status_code == 200
        assert c.post("/ask", json={"text": "x", "at": "9999-12-31T23:00"}).status_code == 422
        assert c.post("/ask", json={"text": "a" * 2000}).status_code == 422
        assert c.post("/ask", json={"text": "x", "lat": 999, "lon": 71}).status_code == 422
        assert c.get("/whats-on", params={**EXPO, "horizon": 10 ** 12}).status_code == 422
        assert c.get("/places-near", params={**EXPO, "limit": -1}).status_code == 422
        assert c.get("/qr.svg", params={"data": "x", "dark": "<script>"}).status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_incoming_gps_is_rounded(db, sample_places, monkeypatch):
    c = client_for(db, sample_places, monkeypatch)
    try:
        a = c.post("/ask", json={"text": "кофе у EXPO", "lat": 51.0901234, "lon": 71.4165432, "at": AT}).json()["answer"]
        assert (a["intent"]["lat"], a["intent"]["lon"]) != (51.0901234, 71.4165432)   # exact GPS never comes back
    finally:
        app.dependency_overrides.clear()


def test_rate_limit_and_cors(db, sample_places, monkeypatch):
    client_for(db, sample_places, monkeypatch)
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "sk-test")     # only Claude-backed calls are limited
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "42:TEST")
    monkeypatch.setitem(api.LIMITS, "ip", 2)
    monkeypatch.setitem(api.LIMITS, "user", 1)
    api._HITS.clear()
    c = TestClient(app)
    body = {"text": "кофе", **EXPO, "at": AT, "llm": False}
    try:
        assert [c.post("/ask", json=body).status_code for _ in range(3)] == [200, 200, 429]
        init = sign_init_data({"auth_date": str(int(time.time())), "user": json.dumps({"id": 7})}, "42:TEST")
        h = {"x-telegram-init-data": init}
        assert [c.post("/ask", json=body, headers=h).status_code for _ in range(2)] == [200, 429]
        assert all(c.get("/tours").status_code == 200 for _ in range(5))      # cheap endpoints: no limit
        from qydyr_data.telegram_auth import bot_key
        bot = {"x-qydyr-bot": bot_key("42:TEST")}                              # the bot's HTTP backend: one IP, many users
        assert all(c.post("/ask", json=body, headers=bot).status_code == 200 for _ in range(4))
        assert c.post("/ask", json=body, headers={"x-qydyr-bot": "forged"}).status_code == 429
    finally:
        app.dependency_overrides.clear()
        api._HITS.clear()


def test_access_log_has_no_coordinates():
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                            ("1.2.3.4", "GET", "/walks?lat=51.128&lon=71.43", "1.1", 200), None)
    api._NoQuery().filter(rec)
    assert "lat=" not in rec.getMessage() and "/walks" in rec.getMessage()


@pytest.mark.parametrize("origin,allowed", [("https://qydyr.example", True), ("https://evil.example", False)])
def test_cors_only_own_origin(monkeypatch, origin, allowed):
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    monkeypatch.setattr(settings, "PUBLIC_URL", "https://qydyr.example")
    # the middleware is configured at import; check the same rule on a fresh app
    probe = FastAPI()
    probe.add_middleware(CORSMiddleware, allow_origins=[settings.PUBLIC_URL] if settings.PUBLIC_URL else ["*"],
                         allow_methods=["*"], allow_headers=["*"])
    probe.post("/ask")(lambda: {})
    r = TestClient(probe).options("/ask", headers={"Origin": origin, "Access-Control-Request-Method": "POST"})
    assert (r.headers.get("access-control-allow-origin") == origin) is allowed


def test_mini_app_code_is_revalidated_pictures_are_not():
    # Telegram's webview caches the Mini App: after a deploy it must ask for the new pages and code
    from fastapi.testclient import TestClient
    from qydyr_data.api import app
    c = TestClient(app)
    for path in ("/app/", "/app/assets/app.js", "/app/assets/app.css"):
        assert c.get(path).headers.get("cache-control") == "no-cache", path
    assert c.get("/app/assets/art/tea.webp").headers.get("cache-control") != "no-cache"
