PY ?= python3

.PHONY: install db places events data demo-data stats eval serve demo bot api mcp test docker tunnel

install:            ## dependencies
	$(PY) -m pip install -r requirements.txt

db:                 ## create / update the schema (idempotent)
	$(PY) -m qydyr_data init-db

places:             ## all POI of Astana from OpenStreetMap
	$(PY) -m qydyr_data places

events:             ## afishas, venue sites, Telegram channels -> events
	$(PY) -m qydyr_data events

data: places events stats

demo-data:          ## the fixture city, no internet needed
	$(PY) -m qydyr_data demo-data

stats:
	$(PY) -m qydyr_data stats

eval:               ## brief scenarios on the current data
	$(PY) -m qydyr_data eval

serve:              ## API + Mini App (/app) + Telegram bot in one process
	$(PY) -m qydyr_data serve --port 8000

demo:               ## the same, frozen at Friday 19:10 with fixture weather (for rehearsals and the video)
	QYDYR_DEMO_NOW=2026-10-23T19:10 QYDYR_WEATHER_FILE=tests/fixtures/weather_mild.json $(PY) -m qydyr_data serve --port 8000

bot:
	$(PY) -m qydyr_data bot

api:
	uvicorn qydyr_data.api:app --reload --port 8000

mcp:                ## Astana City MCP over stdio (Claude Desktop)
	$(PY) -m qydyr_data mcp

test:
	$(PY) -m pytest -q

docker:             ## PostGIS + app in containers
	docker compose up --build

tunnel:             ## a temporary https URL for the Mini App (Cloudflare quick tunnel)
	cloudflared tunnel --url http://localhost:8000
