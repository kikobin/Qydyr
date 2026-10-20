"""Runtime settings. Everything can be overridden with environment variables (see .env.example)."""
from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env support (KEY=VALUE lines); real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")

CONFIG_DIR = ROOT / "config"
SQL_DIR = ROOT / "sql"
DATA_DIR = Path(os.getenv("QYDYR_DATA_DIR", ROOT / "data"))

TZ_NAME = os.getenv("QYDYR_TZ", "Asia/Almaty")  # Astana, UTC+5
TZ = ZoneInfo(TZ_NAME)
COUNTRY = "KZ"  # for public holidays (PH) in opening_hours

# south, west, north, east — whole city incl. left and right bank
ASTANA_BBOX = (50.98, 71.15, 51.32, 71.72)

USER_AGENT = os.getenv(
    "QYDYR_USER_AGENT",
    "QydyrBot/0.1 (city walking assistant prototype; Build with AI hackathon, Astana)",
)
DATABASE_URL = os.getenv("DATABASE_URL")
DB_CONNECT_TIMEOUT_S = int(os.getenv("QYDYR_DB_CONNECT_TIMEOUT", "10"))
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
DGIS_API_KEY = os.getenv("QYDYR_DGIS_KEY") or os.getenv("DGIS_API_KEY")   # 2ГИС Places API: hours, ratings, more places
# Haiku 5.5: high-volume extraction and request understanding. Sonnet 5.5: translations, photos.
CLAUDE_MODEL = os.getenv("QYDYR_CLAUDE_MODEL", "claude-haiku-5-5")
CLAUDE_MODEL_SMART = os.getenv("QYDYR_CLAUDE_MODEL_SMART", "claude-sonnet-5-5")
CLAUDE_MODEL_NARRATE = os.getenv("QYDYR_CLAUDE_MODEL_NARRATE", CLAUDE_MODEL_SMART)  # the "why" people read

# Telegram bot + Mini App
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
BOT_USERNAME = os.getenv("QYDYR_BOT_USERNAME", "")              # without @, for deep links and QR codes
PUBLIC_URL = os.getenv("QYDYR_PUBLIC_URL", "").rstrip("/")      # https://… where the API and the Mini App live
API_URL = os.getenv("QYDYR_API_URL", "")                       # the bot talks to this API; empty = in-process

# Map styles for the Mini App (MapLibre GL). Free CARTO basemaps with OSM data; replace with your own.
MAP_STYLE_DARK = os.getenv("QYDYR_MAP_STYLE_DARK", "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json")
MAP_STYLE_LIGHT = os.getenv("QYDYR_MAP_STYLE_LIGHT", "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json")
WEB_DIR = ROOT / "qydyr_data" / "web"
TOURS_FILE = Path(os.getenv("QYDYR_TOURS_FILE") or ROOT / "config" / "tours.yaml")   # guided walks with sourced facts

# Demo mode (rehearsals, video recording, offline laptops): a frozen "now" and weather from files.
DEMO_NOW = os.getenv("QYDYR_DEMO_NOW")              # e.g. 2026-10-23T19:10
WEATHER_FILE = os.getenv("QYDYR_WEATHER_FILE")      # Open-Meteo JSON instead of the live API
AIR_FILE = os.getenv("QYDYR_AIR_FILE")              # Open-Meteo air-quality JSON instead of the live API

HOURS_HORIZON_DAYS = int(os.getenv("QYDYR_HOURS_DAYS", "14"))    # place_hours materialization
EVENTS_HORIZON_DAYS = int(os.getenv("QYDYR_EVENTS_DAYS", "45"))  # event_sessions materialization
FETCH_DELAY_S = float(os.getenv("QYDYR_FETCH_DELAY", "1.5"))     # per-host politeness delay
CACHE_TTL_H = float(os.getenv("QYDYR_CACHE_TTL_H", "5"))
