-- Qydyr data layer: places + events.
-- PostgreSQL 14+ with PostGIS 3. Works as-is on Supabase (extensions postgis, pg_trgm).

create extension if not exists postgis;
create extension if not exists pg_trgm;

-- Where every row came from. One row per configured source (OSM, afisha sites, Telegram channels...).
create table if not exists sources (
  id           text primary key,
  kind         text not null check (kind in ('osm', 'listing', 'article_index', 'page', 'telegram', 'manual')),
  url          text,
  title        text,
  reliability  real not null default 0.8 check (reliability between 0 and 1),
  last_run_at  timestamptz,
  last_status  text,
  created_at   timestamptz not null default now()
);

-- Points of interest (from OpenStreetMap + overrides).
create table if not exists places (
  id             text primary key,                 -- 'osm:node/123'
  name           text not null,
  name_ru        text,
  name_kk        text,
  name_en        text,
  aliases        text[] not null default '{}',
  category       text not null,                    -- food, culture, park, kids, landmark, nightlife, ...
  subcategory    text,                             -- cafe, museum, playground, viewpoint, ...
  geom           geography(Point, 4326) not null,
  address        text,
  website        text,
  phone          text,
  opening_hours  text,                             -- raw OSM opening_hours syntax
  hours_valid    boolean not null default false,   -- parsed successfully
  hours_until    timestamptz,                      -- place_hours materialized up to this moment
  indoor         text not null default 'unknown' check (indoor in ('indoor', 'outdoor', 'mixed', 'unknown')),
  attrs          jsonb not null default '{}'::jsonb, -- wheelchair, wifi, outdoor_seating, cuisine, ...
  source_id      text not null references sources(id),
  source_url     text,
  fetched_at     timestamptz not null,
  updated_at     timestamptz not null default now()
);
create index if not exists places_geom_idx      on places using gist (geom);
create index if not exists places_name_trgm_idx on places using gin (lower(name) gin_trgm_ops);
create index if not exists places_category_idx  on places (category);

-- Opening intervals for the next N days, computed in Python from opening_hours
-- (incl. Kazakhstan public holidays). Lets SQL answer "open now / closes in 30 min".
create table if not exists place_hours (
  place_id   text not null references places(id) on delete cascade,
  opens_at   timestamptz not null,
  closes_at  timestamptz not null,
  primary key (place_id, opens_at),
  check (closes_at > opens_at)
);
create index if not exists place_hours_lookup_idx on place_hours (place_id, opens_at, closes_at);

-- Events: exhibitions, concerts, festivals, workshops...
create table if not exists events (
  id             text primary key,                 -- stable hash(title | venue | first date)
  title          text not null,
  description    text,
  kind           text not null default 'other' check (kind in (
                   'exhibition', 'concert', 'performance', 'festival', 'fair', 'workshop', 'lecture',
                   'party', 'excursion', 'sport', 'kids', 'film', 'standup', 'other')),
  is_temporary   boolean not null default false,   -- has an end date: temporary exhibition, festival, pop-up
  place_id       text references places(id) on delete set null,
  venue_name     text,
  address        text,
  geom           geography(Point, 4326),           -- from the matched place, JSON-LD geo or geocoding
  starts_on      date,
  ends_on        date,
  schedule_text  text,                             -- recurring rule in OSM opening_hours syntax
  price_text     text,
  price_min      numeric,
  currency       text default 'KZT',
  is_free        boolean,
  age_limit      text,
  url            text,
  image_url      text,
  status         text not null default 'scheduled' check (status in ('scheduled', 'cancelled', 'postponed')),
  extraction     text not null check (extraction in ('jsonld', 'llm', 'telegram', 'manual')),
  evidence       text,                             -- verbatim snippet from the source proving the date/time
  evidence_ok    boolean not null default false,   -- snippet really found in the source text
  confidence     real not null default 0.5 check (confidence between 0 and 1),
  sources        jsonb not null default '[]'::jsonb, -- [{source_id, url, extraction, seen_at}]
  first_seen_at  timestamptz not null default now(),
  last_seen_at   timestamptz not null default now()
);
create index if not exists events_geom_idx  on events using gist (geom);
create index if not exists events_dates_idx on events (starts_on, ends_on);
create index if not exists events_place_idx on events (place_id);

-- Concrete time windows of an event. A two-month exhibition becomes one row per open day.
create table if not exists event_sessions (
  event_id       text not null references events(id) on delete cascade,
  starts_at      timestamptz not null,
  ends_at        timestamptz not null,
  time_known     boolean not null default true,    -- false: only the date is known
  end_estimated  boolean not null default false,   -- end guessed from typical duration of this kind
  hours_from     text not null default 'event' check (hours_from in ('event', 'venue', 'none')),
  primary key (event_id, starts_at),
  check (ends_at > starts_at)
);
create index if not exists event_sessions_time_idx on event_sessions (starts_at, ends_at);
