"""CLI: python -m qydyr_data <command>

  init-db                     apply sql/*.sql
  places [--from-file F]      OpenStreetMap -> places + opening intervals
  events [--source ID] [--no-llm] [--no-geocode] [--prune]
                              afishas / venue pages / Telegram -> events + sessions
  dgis [--mode] [--limit N]   2ГИС: hours, ratings, Wi-Fi, step-free entrances, places OSM lacks
  dgis --export | --from-file  move the 2ГИС answers to another database (production) without the API
  dgis-probe                  one request to each 2ГИС API, raw answers to data/dgis_probe.zip
  audit [--scenario ID]       the city page: where typical requests find nothing nearby
  run-all                     places (if older than a day) + events — what the cron job runs
  seed                        a new database on its first start: only what is missing (the container runs it)
  whats-on --lat --lon        what is on nearby now (or --at "2026-10-23 19:10")
  near --lat --lon            places nearby with open/closed state
  ask "текст" [--lat --lon]   the assistant: 1–3 picks with reasons, mini-route, weather, deadline
  eval [--file F]             run scenarios (the brief's five and more), check every answer
  stats                       data quality report
  unmatched                   event venues not found on the map (add them as aliases)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import settings, store
from .badges import confidence_word, describe_event, describe_place

log = logging.getLogger("qydyr")


def _at(s: str | None) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=settings.TZ)


def cmd_init_db(args) -> None:
    with store.connect() as conn:
        store.init_db(conn)
    print("schema applied")


def cmd_places(args) -> None:
    from .places import overpass, overrides
    from .places.normalize import normalize_overpass

    if args.from_file:
        data = json.loads(Path(args.from_file).read_text(encoding="utf-8"))
    else:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M")
        data = overpass.fetch(save_raw_to=settings.DATA_DIR / "raw" / f"overpass_{ts}.json")
    from .places import dgis
    places = normalize_overpass(data)
    touched = overrides.apply(places, overrides.load())
    with store.connect() as conn:
        enriched, added = dgis.apply(conn, places)   # what 2ГИС added on earlier runs stays after an OSM refresh
        n = store.upsert_places(conn, places)
    print(f"places: {n} upserted ({touched} curated by overrides, {enriched} enriched and {added} added from 2ГИС)")


def cmd_dgis(args) -> None:
    """2ГИС: ask (within --limit requests), store, merge into places."""
    import httpx

    from .places import dgis
    if args.export or args.from_file:   # no API: move what was asked between databases
        with store.connect() as conn:
            if args.export:
                print(f"2ГИС: {dgis.export_items(conn, Path(args.export))} items -> {args.export}")
                return
            print(f"2ГИС: {dgis.import_items(conn, Path(args.from_file))} items <- {args.from_file}")
            places = store.load_places(conn)
            enriched, added = dgis.apply(conn, places)
            store.upsert_places(conn, places)
        print(f"places: {enriched} enriched, {added} added")
        return
    args.tier = args.tier or [1, 2, 3]
    client = dgis.DgisClient(delay_s=args.delay)
    with store.connect() as conn:
        got = {}
        try:
            if args.mode in ("rubrics", "all"):
                got["rubrics"] = dgis.import_rubrics(conn, client, args.limit, tiers=tuple(args.tier))
            if args.mode in ("enrich", "all"):
                got["enrich"] = dgis.enrich(conn, client, args.limit)
            if args.mode == "discover":
                got["discover"] = dgis.discover(conn, client, args.limit)
        except (RuntimeError, httpx.HTTPError) as e:   # quota, a blocked key, no network: keep what was found
            conn.rollback()
            print(f"2ГИС stopped: {e}")
        places = store.load_places(conn)
        enriched, added = dgis.apply(conn, places)
        store.upsert_places(conn, places)
    print(f"2ГИС: {client.requests} requests; {got}; places: {enriched} enriched, {added} added")


def cmd_events(args) -> None:
    from .events.build import build_records
    from .events.crawl import Collector, load_sources
    from .fetch import PoliteFetcher

    sources = load_sources()
    if args.source:
        sources = [s for s in sources if s.id in args.source]
    extractor = None
    if not args.no_llm:
        from .events.llm import ClaudeExtractor
        extractor = ClaudeExtractor()
    collector = Collector(PoliteFetcher(), extractor)
    run_started = datetime.now(timezone.utc)
    raws, stats = collector.collect(sources)

    with store.connect() as conn:
        places = store.load_places_lite(conn)
        records = build_records(raws, places, today=collector.today)
        if not args.no_geocode:
            from .events.geocode import Geocoder
            geo = Geocoder()
            for r in records:
                if not r.place_id and r.lat is None and (hit := geo.locate(r.address)):
                    r.lat, r.lon = hit
                    r.match_method = "geocode"
            geo.save()
        store.upsert_events(conn, records)
        by_id = {s.id: s for s in sources}
        for st in stats:
            src = by_id[st.source_id]
            store.upsert_source(conn, id=src.id, kind=src.kind, url=src.url, title=src.title,
                                reliability=src.reliability, status=st.status)
        if args.prune:
            prunable = {s.id for s in sources if s.kind in ("listing", "page")}
            ok = [st.source_id for st in stats if st.source_id in prunable and not st.errors]
            n = store.prune_events(conn, ok, run_started)
            print(f"pruned {n} events no longer listed by: {', '.join(ok) or '-'}")
        conn.commit()

    out = settings.DATA_DIR / "out" / f"events_{datetime.now(timezone.utc):%Y%m%dT%H%M}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(r.model_dump_json() for r in records), encoding="utf-8")
    matched = sum(1 for r in records if r.place_id)
    print(f"events: {len(raws)} extracted -> {len(records)} unique, {matched} matched to places; saved {out}")


def cmd_run_all(args) -> None:
    with store.connect() as conn:
        row = conn.execute("select max(fetched_at) last from places").fetchone()
    last = row["last"] if row else None
    if args.force_places or last is None or (datetime.now(timezone.utc) - last).total_seconds() > 20 * 3600:
        cmd_places(argparse.Namespace(from_file=None))
    cmd_events(argparse.Namespace(source=None, no_llm=args.no_llm, no_geocode=False, prune=False))


def cmd_seed(args) -> None:
    """A new database fills itself on the first start (Railway, Render): what is missing is collected or loaded,
    what is there is left alone. The container runs it next to the server, so the health check never waits."""
    import hashlib

    from .places import dgis
    with store.connect() as conn:
        n = conn.execute("""select (select count(*) from places where source_id = 'osm') osm,
                                   (select count(*) from events) events, (select count(*) from city_audit) audit""").fetchone()
    log.info("seed: %s", dict(n))
    changed = False
    if not n["osm"]:
        try:
            cmd_places(argparse.Namespace(from_file=None))
            changed = True
        except Exception as e:     # Overpass busy: the 2ГИС places and the next run still help
            log.warning("seed: places failed: %s", e)
    seed = Path(args.file)
    if seed.exists():
        key = "seed:" + hashlib.sha256(seed.read_bytes()).hexdigest()[:16]
        with store.connect() as conn:
            if not conn.execute("select 1 from dgis_lookups where key = %s", (key,)).fetchone():
                k = dgis.import_items(conn, seed)
                places = store.load_places(conn)
                enriched, added = dgis.apply(conn, places)
                store.upsert_places(conn, places)
                dgis._remember(conn, key, k)
                conn.commit()
                log.info("seed: 2ГИС %d items, %d places enriched, %d added", k, enriched, added)
                changed = True
    if not n["events"]:
        try:
            cmd_events(argparse.Namespace(source=None, no_llm=not settings.ANTHROPIC_API_KEY, no_geocode=False,
                                          prune=False))
        except Exception as e:
            log.warning("seed: events failed: %s", e)
    if not n["audit"] or changed:   # the city page needs it, and new places change it
        cmd_audit(argparse.Namespace(scenario=None))
    log.info("seed: done")


def cmd_whats_on(args) -> None:
    with store.connect() as conn:
        rows = store.whats_on(conn, args.lat, args.lon, args.radius, _at(args.at), args.horizon)
    if args.json:
        print(json.dumps(rows, default=str, ensure_ascii=False, indent=2))
        return
    if not rows:
        print("Рядом сейчас ничего не нашлось.")
    for r in rows:
        print(f"• {r['title']} — {r['venue'] or 'площадка не определена'} ({r['distance_m']} м)\n"
              f"  {describe_event(r)}\n"
              f"  уверенность: {confidence_word(r['confidence'])} ({r['confidence']:.2f}),"
              f" источник: {r['url']}")


def cmd_near(args) -> None:
    cats = args.categories.split(",") if args.categories else None
    with store.connect() as conn:
        rows = store.places_near(conn, args.lat, args.lon, args.radius, _at(args.at), cats, args.limit)
    for r in rows:
        print(f"• {r['name']} [{r['category']}/{r['subcategory']}, {r['indoor']}] {r['distance_m']} м — {describe_place(r)}")


def cmd_stats(args) -> None:
    with store.connect() as conn:
        print(json.dumps(store.stats(conn), default=str, ensure_ascii=False, indent=2))


def cmd_ask(args) -> None:
    from .assistant.recommend import Assistant, DbData
    from .assistant.render import render, render_why_not

    llm = narrator = None
    if args.llm:
        from .assistant.intent import ClaudeIntentParser
        from .assistant.narrate import Narrator
        llm, narrator = ClaudeIntentParser(), Narrator()
    with store.connect() as conn:
        from .places.dgis import Geocoder
        a = Assistant(DbData(conn), llm=llm, narrator=narrator, geocode=Geocoder.default())
        ans = a.ask(args.text, at=_at(args.at), lat=args.lat, lon=args.lon,
                    weather_file=Path(args.weather_file) if args.weather_file else None,
                    air_file=Path(args.air_file) if args.air_file else None,
                    fetch_weather=not args.no_weather, members=args.member, weather_preset=args.preset,
                    surprise=args.surprise)
    if args.json:
        print(ans.model_dump_json(indent=2))
        return
    text = render(ans)
    if args.why_not:
        text += "\n\n" + render_why_not(ans)
    if ans.intent.lang != "ru":
        from .assistant.translate import translate
        text = translate(text, ans.intent.lang) or text
    print(text)


def cmd_eval(args) -> None:
    from .assistant.evaluate import run_file

    with store.connect() as conn:
        results = run_file(conn, Path(args.file))
    width = max(len(r.id) for r in results)
    for r in results:
        print(f"{'OK  ' if r.ok else 'FAIL'} {r.id:<{width}}  {r.summary}")
        for f in r.failures:
            print(f"      - {f}")
    passed = sum(r.ok for r in results)
    print(f"\n{passed}/{len(results)} сценариев прошли все проверки")
    if passed < len(results):
        sys.exit(1)


def cmd_demo_data(args) -> None:
    from . import demo
    with store.connect() as conn:
        store.init_db(conn)
        raws, records, stats, _ = demo.load(conn)
        conn.commit()
    print(f"demo data: {len(demo.demo_places())} places, {len(records)} events. Run the app in demo mode:\n"
          "  QYDYR_DEMO_NOW=2026-10-23T19:10 QYDYR_WEATHER_FILE=tests/fixtures/weather_mild.json python -m qydyr_data serve")


def cmd_bot(args) -> None:
    import asyncio

    from .bot.app import run_bot
    asyncio.run(run_bot())


def cmd_serve(args) -> None:
    """API + Mini App + Telegram bot in one process: the bot and the app share answers and group state."""
    import asyncio

    import uvicorn

    async def main() -> None:
        server = uvicorn.Server(uvicorn.Config("qydyr_data.api:app", host=args.host, port=args.port,
                                               proxy_headers=True, forwarded_allow_ips="*", log_level="info"))
        tasks = [server.serve()]
        if settings.TELEGRAM_BOT_TOKEN:
            from .bot.app import run_bot
            tasks.append(run_bot())
        else:
            logging.getLogger("qydyr").warning("TELEGRAM_BOT_TOKEN is not set: serving the API and the Mini App only")
        await asyncio.gather(*tasks)

    asyncio.run(main())


def cmd_mcp(args) -> None:
    from .mcp_server import main as mcp_main
    mcp_main(transport=args.transport)


def cmd_audit(args) -> None:
    """Qydyr for the city: ask the typical requests in every populated ~1 km cell, store the gaps."""
    from . import city
    scenarios = [city.BY_ID[s] for s in args.scenario] if args.scenario else None
    with store.connect() as conn:
        gaps = city.run_audit(conn, scenarios)
    for sid, n in gaps.items():
        print(f"{sid}: {n} cells where the request has no close, confirmed answer")


def cmd_unmatched(args) -> None:
    with store.connect() as conn:
        rows = store.unmatched_venues(conn, args.limit)
    if not rows:
        print("Все площадки событий найдены на карте.")
    for r in rows:
        print(f"{r['events']:>3} × {r['venue_name']}   ({r['example']})")
    if rows:
        print("\nДобавьте эти названия как aliases в config/place_overrides.yaml и перезапустите events.")


def main(argv: list[str] | None = None) -> None:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1251/cp866: ₸, −, emoji
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)   # its INFO line prints request URLs, API keys included
    p = argparse.ArgumentParser(prog="qydyr_data", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db").set_defaults(fn=cmd_init_db)

    sp = sub.add_parser("places")
    sp.add_argument("--from-file", help="use a saved Overpass JSON instead of querying the API")
    sp.set_defaults(fn=cmd_places)

    se = sub.add_parser("events")
    se.add_argument("--source", action="append", help="only these source ids (repeatable)")
    se.add_argument("--no-llm", action="store_true", help="JSON-LD only, no Claude calls")
    se.add_argument("--no-geocode", action="store_true", help="don't geocode addresses of unmatched venues")
    se.add_argument("--prune", action="store_true",
                    help="delete events that sources crawled OK in this run no longer list (not Telegram/digests)")
    se.set_defaults(fn=cmd_events)

    sr = sub.add_parser("run-all")
    sr.add_argument("--no-llm", action="store_true")
    sr.add_argument("--force-places", action="store_true")
    sr.set_defaults(fn=cmd_run_all)

    for name, fn in (("whats-on", cmd_whats_on), ("near", cmd_near)):
        s = sub.add_parser(name)
        s.add_argument("--lat", type=float, required=True)
        s.add_argument("--lon", type=float, required=True)
        s.add_argument("--radius", type=float, default=1500 if name == "whats-on" else 800)
        s.add_argument("--at", help='local time, e.g. "2026-10-23 19:10"')
        if name == "whats-on":
            s.add_argument("--horizon", type=int, default=180, help="minutes ahead for 'soon'")
            s.add_argument("--json", action="store_true")
        else:
            s.add_argument("--categories", help="comma-separated: food,culture,park,kids,...")
            s.add_argument("--limit", type=int, default=30)
        s.set_defaults(fn=fn)

    sa = sub.add_parser("ask", help="ask the assistant like a user would")
    sa.add_argument("text")
    sa.add_argument("--lat", type=float)
    sa.add_argument("--lon", type=float)
    sa.add_argument("--at", help='local time, e.g. "2026-10-23 15:00"')
    sa.add_argument("--weather-file", help="Open-Meteo JSON instead of the live API")
    sa.add_argument("--no-weather", action="store_true")
    sa.add_argument("--air-file", help="Open-Meteo air-quality JSON instead of the live API")
    sa.add_argument("--preset", choices=["blizzard", "frost", "rain", "summer_evening", "smog"],
                    help="time & weather machine: what-if weather")
    sa.add_argument("--surprise", action="store_true", help="'Удиви меня': one unexpected pick")
    sa.add_argument("--why-not", action="store_true", help="also print why other places were dropped")
    sa.add_argument("--member", action="append", help='group mode: "Айгерим: кофе и тихо" (repeatable)')
    sa.add_argument("--llm", action="store_true", help="also use Claude: understand the request, word the answer")
    sa.add_argument("--json", action="store_true")
    sa.set_defaults(fn=cmd_ask)

    sv = sub.add_parser("eval", help="run test scenarios and check every answer against hard rules")
    sv.add_argument("--file", default=str(settings.ROOT / "eval" / "scenarios.yaml"))
    sv.set_defaults(fn=cmd_eval)

    sub.add_parser("stats").set_defaults(fn=cmd_stats)
    sub.add_parser("bot", help="run the Telegram bot (polling)").set_defaults(fn=cmd_bot)
    sub.add_parser("demo-data", help="load the fixture city (places + events) for rehearsals and offline demos").set_defaults(fn=cmd_demo_data)
    ss = sub.add_parser("serve", help="API + Mini App + bot in one process")
    ss.add_argument("--host", default="0.0.0.0")
    ss.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    ss.set_defaults(fn=cmd_serve)
    sm = sub.add_parser("mcp", help="Astana City MCP server (for Claude Desktop and other MCP clients)")
    sm.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    sm.set_defaults(fn=cmd_mcp)
    sd = sub.add_parser("dgis", help="2ГИС Places API: hours, ratings, Wi-Fi, step-free entrances, more places")
    sd.add_argument("--mode", choices=["rubrics", "enrich", "discover", "all"], default="all",
                    help="rubrics: every branch of the rubrics people go out to (the main one); enrich: the OSM places "
                         "still unmatched, by name; discover: free-text queries around cells")
    sd.add_argument("--tier", type=int, action="append", default=None,
                    help="rubric tiers to import: 1 places to go (kids, museums, coworkings...), 2 coffee shops, "
                         "restaurants, bars, 3 cafés and fast food. Default: all")
    sd.add_argument("--delay", type=float, default=0.6, help="seconds between requests (the server drops fast clients)")
    sd.add_argument("--limit", type=int, default=500, help="network requests this run (the key has a quota)")
    sd.add_argument("--export", nargs="?", const=str(settings.DATA_DIR / "dgis_items.jsonl.gz"),
                    help="write what was asked so far to a file (default data/dgis_items.jsonl.gz), no API")
    sd.add_argument("--from-file", nargs="?", const=str(settings.DATA_DIR / "dgis_items.jsonl.gz"),
                    help="load such a file into this database and merge it into places, no API and no key")
    sd.set_defaults(fn=cmd_dgis)
    sx = sub.add_parser("seed", help="fill a new database: places, 2ГИС from seed/, events, city audit — only what is missing")
    sx.add_argument("--file", default=str(settings.SEED_DIR / "dgis_items.jsonl.gz"))
    sx.set_defaults(fn=cmd_seed)
    sub.add_parser("dgis-probe", help="one request to each 2ГИС API, raw answers to data/dgis_probe.zip").set_defaults(
        fn=lambda a: print(__import__("qydyr_data.places.dgis_probe", fromlist=["run"]).run()))
    sc = sub.add_parser("audit", help="city audit: where typical requests find nothing nearby (for /app/city.html)")
    sc.add_argument("--scenario", action="append", help="kids_evening, work_quiet, step_free, night, frost, events")
    sc.set_defaults(fn=cmd_audit)
    su = sub.add_parser("unmatched")
    su.add_argument("--limit", type=int, default=30)
    su.set_defaults(fn=cmd_unmatched)

    args = p.parse_args(argv)
    try:
        args.fn(args)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
