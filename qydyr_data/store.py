"""PostgreSQL / PostGIS storage (Supabase-compatible)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, time, timedelta, timezone

import psycopg
from psycopg.rows import dict_row

from . import hours, settings
from .events.models import EventRecord
from .events.venues import PlaceLite
from .places.normalize import Place

log = logging.getLogger(__name__)


def connect(dsn: str | None = None) -> psycopg.Connection:
    dsn = dsn or settings.DATABASE_URL
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set")
    # without a timeout an unreachable host (dropped packets, sleeping Supabase) hangs forever
    return psycopg.connect(dsn, row_factory=dict_row, connect_timeout=settings.DB_CONNECT_TIMEOUT_S)


def init_db(conn: psycopg.Connection) -> None:
    for f in sorted(settings.SQL_DIR.glob("*.sql")):
        log.info("applying %s", f.name)
        conn.execute(f.read_text(encoding="utf-8"))
    conn.commit()


def upsert_source(conn, *, id: str, kind: str, url: str | None, title: str | None, reliability: float,
                  status: str | None = None) -> None:
    conn.execute(
        """insert into sources (id, kind, url, title, reliability, last_run_at, last_status)
           values (%s, %s, %s, %s, %s, now(), %s)
           on conflict (id) do update set kind = excluded.kind, url = excluded.url, title = excluded.title,
             reliability = excluded.reliability, last_run_at = now(), last_status = excluded.last_status""",
        (id, kind, url, title, reliability, status),
    )


# ---------------- places ----------------
def upsert_places(conn, places: list[Place], now: datetime | None = None,
                  horizon_days: int = settings.HOURS_HORIZON_DAYS) -> int:
    now = now or datetime.now(timezone.utc)
    start = datetime.combine(now.astimezone(settings.TZ).date(), time.min, tzinfo=settings.TZ)
    until = start + timedelta(days=horizon_days)
    upsert_source(conn, id="osm", kind="osm", url="https://www.openstreetmap.org",
                  title="OpenStreetMap (© OpenStreetMap contributors, ODbL)", reliability=0.85, status=f"ok: {len(places)} places")
    rows, hour_rows = [], []
    for p in places:
        rows.append((p.id, p.name, p.name_ru, p.name_kk, p.name_en, p.aliases, p.category, p.subcategory,
                     p.lon, p.lat, p.address, p.website, p.phone, p.opening_hours, p.hours_valid,
                     until if p.hours_valid else None, p.indoor, json.dumps(p.attrs, ensure_ascii=False),
                     p.source_id, p.source_url, p.fetched_at))
        if p.hours_valid:
            for a, b in hours.open_intervals(p.opening_hours, start, until):
                hour_rows.append((p.id, a, b))
    with conn.cursor() as cur:
        cur.executemany(
            """insert into places (id, name, name_ru, name_kk, name_en, aliases, category, subcategory, geom,
                 address, website, phone, opening_hours, hours_valid, hours_until, indoor, attrs,
                 source_id, source_url, fetched_at, updated_at)
               values (%s,%s,%s,%s,%s,%s,%s,%s, st_setsrid(st_makepoint(%s,%s),4326)::geography,
                 %s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s, now())
               on conflict (id) do update set name = excluded.name, name_ru = excluded.name_ru,
                 name_kk = excluded.name_kk, name_en = excluded.name_en, aliases = excluded.aliases,
                 category = excluded.category, subcategory = excluded.subcategory, geom = excluded.geom,
                 address = excluded.address, website = excluded.website, phone = excluded.phone,
                 opening_hours = excluded.opening_hours, hours_valid = excluded.hours_valid,
                 hours_until = excluded.hours_until, indoor = excluded.indoor, attrs = excluded.attrs,
                 source_url = excluded.source_url, fetched_at = excluded.fetched_at, updated_at = now()""",
            rows,
        )
        ids = [p.id for p in places]
        cur.execute("delete from place_hours where place_id = any(%s)", (ids,))
        cur.executemany("insert into place_hours (place_id, opens_at, closes_at) values (%s,%s,%s)"
                        " on conflict do nothing", hour_rows)
    conn.commit()
    log.info("upserted %d places, %d opening intervals", len(rows), len(hour_rows))
    return len(rows)


def load_places_lite(conn) -> list[PlaceLite]:
    rows = conn.execute(
        """select id, name, name_ru, name_kk, name_en, aliases, category, subcategory, address, opening_hours,
                  st_y(geom::geometry) as lat, st_x(geom::geometry) as lon
           from places where category <> 'kids' or subcategory <> 'playground'"""
    ).fetchall()
    return [PlaceLite(id=r["id"], name=r["name"], lat=r["lat"], lon=r["lon"], category=r["category"],
                      name_ru=r["name_ru"], name_kk=r["name_kk"], name_en=r["name_en"], aliases=r["aliases"] or [],
                      address=r["address"], opening_hours=r["opening_hours"], subcategory=r["subcategory"])
            for r in rows]


# ---------------- events ----------------
def prune_events(conn, source_ids: list[str], seen_before: datetime) -> int:
    """Delete events not seen since `seen_before` whose every source is in `source_ids` (crawled OK just now).
    An event that is no longer listed anywhere we looked, or was re-keyed (venue matched differently), is gone."""
    if not source_ids:
        return 0
    cur = conn.execute(
        """delete from events e
           where e.last_seen_at < %s
             and jsonb_array_length(e.sources) > 0
             and not exists (select 1 from jsonb_array_elements(e.sources) s
                             where not (s->>'source_id' = any(%s)))""",
        (seen_before, source_ids),
    )
    return cur.rowcount


def upsert_events(conn, records: list[EventRecord]) -> int:
    with conn.cursor() as cur:
        for r in records:
            cur.execute(
                """insert into events (id, title, description, kind, is_temporary, place_id, venue_name, address, geom,
                     starts_on, ends_on, schedule_text, price_text, price_min, is_free, age_limit, url, image_url,
                     status, extraction, evidence, evidence_ok, confidence, sources, last_seen_at)
                   values (%(id)s, %(title)s, %(description)s, %(kind)s, %(is_temporary)s, %(place_id)s,
                     %(venue_name)s, %(address)s,
                     case when %(lon)s::float8 is null then null
                          else st_setsrid(st_makepoint(%(lon)s, %(lat)s), 4326)::geography end,
                     %(starts_on)s, %(ends_on)s, %(schedule_text)s, %(price_text)s, %(price_min)s, %(is_free)s,
                     %(age_limit)s, %(url)s, %(image_url)s, %(status)s, %(extraction)s, %(evidence)s,
                     %(evidence_ok)s, %(confidence)s, %(sources)s::jsonb, now())
                   on conflict (id) do update set
                     title = excluded.title, description = coalesce(excluded.description, events.description),
                     kind = excluded.kind, is_temporary = excluded.is_temporary,
                     place_id = coalesce(excluded.place_id, events.place_id),
                     venue_name = coalesce(excluded.venue_name, events.venue_name),
                     address = coalesce(excluded.address, events.address),
                     geom = coalesce(excluded.geom, events.geom),
                     starts_on = excluded.starts_on, ends_on = excluded.ends_on,
                     schedule_text = excluded.schedule_text, price_text = excluded.price_text,
                     price_min = excluded.price_min, is_free = excluded.is_free, age_limit = excluded.age_limit,
                     url = excluded.url, image_url = coalesce(excluded.image_url, events.image_url),
                     status = excluded.status, extraction = excluded.extraction, evidence = excluded.evidence,
                     evidence_ok = excluded.evidence_ok, confidence = excluded.confidence,
                     sources = (select coalesce(jsonb_agg(t.s), '[]'::jsonb) from (
                                  select distinct on (s->>'source_id', s->>'url') s
                                  from jsonb_array_elements(events.sources || excluded.sources) s
                                  order by s->>'source_id', s->>'url', s->>'seen_at' desc) t),
                     last_seen_at = now()""",
                {**r.model_dump(exclude={"sessions", "match_method"}), "sources": json.dumps(r.sources, ensure_ascii=False)},
            )
            cur.execute("delete from event_sessions where event_id = %s", (r.id,))
            cur.executemany(
                """insert into event_sessions (event_id, starts_at, ends_at, time_known, end_estimated, hours_from)
                   values (%s,%s,%s,%s,%s,%s) on conflict do nothing""",
                [(r.id, s.starts_at, s.ends_at, s.time_known, s.end_estimated, s.hours_from) for s in r.sessions],
            )
    conn.commit()
    return len(records)


# ---------------- queries ----------------
def whats_on(conn, lat: float, lon: float, radius_m: float = 1500, at: datetime | None = None,
             horizon_min: int = 180) -> list[dict]:
    return conn.execute(
        "select * from whats_on(%s, %s, %s, coalesce(%s, now()), make_interval(mins => %s), %s)",
        (lat, lon, radius_m, at, horizon_min, settings.TZ_NAME),
    ).fetchall()


def places_near(conn, lat: float, lon: float, radius_m: float = 1000, at: datetime | None = None,
                categories: list[str] | None = None, limit: int = 50) -> list[dict]:
    return conn.execute(
        "select * from places_near(%s, %s, %s, coalesce(%s, now()), %s, %s)",
        (lat, lon, radius_m, at, categories, limit),
    ).fetchall()


def place(conn, place_id: str, at: datetime | None = None) -> dict | None:
    """One place with its open state at `at` (same shape as a places_near() row)."""
    pt = conn.execute("select st_y(geom::geometry) lat, st_x(geom::geometry) lon from places where id = %s",
                      (place_id,)).fetchone()
    if pt is None:
        return None
    rows = places_near(conn, pt["lat"], pt["lon"], 5, at, None, 50)
    return next((r for r in rows if r["place_id"] == place_id), None)


def city_counters(conn, at: datetime | None = None) -> dict:
    """Numbers for the 'Пульс Астаны' screen: what the assistant knows about the city right now."""
    return conn.execute(
        """select (select count(*) from places) places,
                  (select count(*) from places where hours_valid) places_with_hours,
                  (select count(distinct event_id) from event_sessions
                    where (starts_at at time zone %s)::date = (coalesce(%s, now()) at time zone %s)::date) events_today,
                  (select count(*) from sources) sources""",
        (settings.TZ_NAME, at, settings.TZ_NAME),
    ).fetchone()


def unmatched_venues(conn, limit: int = 30) -> list[dict]:
    """Venues we could not place on the map — add them as aliases in config/place_overrides.yaml."""
    return conn.execute(
        """select venue_name, count(*) as events, min(url) as example
           from events where place_id is null and geom is null and venue_name is not null
           group by venue_name order by 2 desc limit %s""",
        (limit,),
    ).fetchall()


def stats(conn) -> dict:
    q = lambda sql: conn.execute(sql).fetchone()  # noqa: E731
    return {
        "places": q("select count(*) n, count(*) filter (where hours_valid) with_hours,"
                    " count(*) filter (where indoor <> 'unknown') with_indoor from places"),
        "places_by_category": conn.execute(
            "select category, count(*) n from places group by 1 order by 2 desc").fetchall(),
        "events": q("select count(*) n, count(*) filter (where place_id is not null) with_place,"
                    " count(*) filter (where is_temporary) temporary, count(*) filter (where status='cancelled') cancelled,"
                    " round(avg(confidence)::numeric, 2) avg_confidence,"
                    " count(*) filter (where evidence_ok) evidence_ok from events"),
        "upcoming_sessions_7d": q("select count(*) n from event_sessions"
                                  " where starts_at between now() and now() + interval '7 days'")["n"],
        "sources": conn.execute("select id, kind, last_run_at, last_status from sources order by id").fetchall(),
    }


# ------------------------------------------------------------------ for the city (sql/003_city.sql)
DEMAND_K = 3   # a cell and need are shown only after this many requests: no single person stands out


def record_demand(conn, cell: str, needs: list[str], unmet: list[str], day) -> None:
    """One request, as counts: the ~1 km cell, the needs understood, which of them no answer covered."""
    for n in dict.fromkeys(needs):
        conn.execute(
            """insert into demand (day, cell, need, asked, unmet) values (%s, %s, %s, 1, %s)
               on conflict (day, cell, need) do update set asked = demand.asked + 1,
                                                          unmet = demand.unmet + excluded.unmet""",
            (day, cell, n, 1 if n in unmet else 0))
    conn.commit()


def demand_summary(conn, days: int = 30, k: int = DEMAND_K) -> list[dict]:
    """Cells and needs people asked for at least k times in the last days, with how often nothing fit."""
    return conn.execute(
        """select cell, need, sum(asked)::int asked, sum(unmet)::int unmet from demand
           where day > current_date - %s group by cell, need having sum(asked) >= %s
           order by sum(unmet) desc, sum(asked) desc""",
        (days, k)).fetchall()


def replace_audit(conn, scenario: str, rows: list[dict]) -> None:
    conn.execute("delete from city_audit where scenario = %s", (scenario,))
    with conn.cursor() as cur:
        cur.executemany(
            """insert into city_audit (scenario, cell, lat, lon, label, places, options, confirmed, nearest_min, best, at)
               values (%(scenario)s, %(cell)s, %(lat)s, %(lon)s, %(label)s, %(places)s, %(options)s, %(confirmed)s,
                       %(nearest_min)s, %(best)s, %(at)s)""",
            [{**r, "scenario": scenario, "best": json.dumps(r["best"], ensure_ascii=False)} for r in rows])
    conn.commit()


def audit(conn, scenario: str) -> list[dict]:
    return conn.execute(
        """select cell, lat, lon, label, places, options, confirmed, nearest_min, best, at, run_at from city_audit
           where scenario = %s order by places desc""", (scenario,)).fetchall()


def data_stats(conn) -> dict:
    """What the assistant stands on: every number on the "Откуда данные" page comes from here."""
    q = lambda sql, *a: conn.execute(sql, a).fetchone()  # noqa: E731
    places = q("""select count(*) n, count(*) filter (where hours_valid) with_hours,
                         count(*) filter (where attrs ? 'opening_hours:source') hours_curated,
                         count(*) filter (where attrs ? 'wheelchair') with_wheelchair,
                         count(*) filter (where attrs->>'wheelchair' = 'yes') wheelchair_yes,
                         count(*) filter (where website is not null or phone is not null) with_contacts,
                         max(fetched_at) fetched_at from places where category <> 'anchor'""")
    by_cat = conn.execute(
        """select category, count(*)::int n, count(*) filter (where hours_valid)::int with_hours,
                  count(*) filter (where attrs ? 'wheelchair')::int with_wheelchair
           from places where category <> 'anchor' group by 1 order by 2 desc""").fetchall()
    events = q("""select count(*) n, count(*) filter (where place_id is not null) with_place,
                         count(*) filter (where place_id is null and geom is not null) geocoded,
                         count(*) filter (where evidence_ok) evidence_ok,
                         count(*) filter (where extraction = 'jsonld') jsonld,
                         count(*) filter (where extraction = 'llm') llm,
                         count(*) filter (where extraction = 'telegram') telegram,
                         max(last_seen_at) last_seen_at from events""")
    by_source = conn.execute(
        """select s.id, s.kind, s.title, s.url, s.last_run_at, s.last_status,
                  (select count(*)::int from events e where e.sources @> jsonb_build_array(jsonb_build_object('source_id', s.id))) events
           from sources s order by events desc, s.id""").fetchall()
    sessions = q("""select count(*) n from event_sessions where starts_at between now() and now() + interval '7 days'""")
    return {"places": places, "places_by_category": by_cat, "events": events, "sources": by_source,
            "sessions_7d": sessions["n"]}
