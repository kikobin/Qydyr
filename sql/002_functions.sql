-- Query functions used by the assistant (RAG retrieval layer).
-- Dropped first so that changing a return type never blocks `init-db`.
drop function if exists whats_on(double precision, double precision, double precision, timestamptz, interval, text);
drop function if exists places_near(double precision, double precision, double precision, timestamptz, text[], integer);

-- What is on near a point: events running now, starting soon, or later today.
-- Returns one row per event (the session that matters most at p_at) with machine labels
-- that the UI / LLM turn into "Сейчас идёт вечерняя выставка", "последние дни" and so on.
create or replace function whats_on(
  p_lat       double precision,
  p_lon       double precision,
  p_radius_m  double precision default 1500,
  p_at        timestamptz      default now(),
  p_horizon   interval         default interval '3 hours',
  p_tz        text             default 'Asia/Almaty'
) returns table (
  event_id          text,
  title             text,
  kind              text,
  venue             text,
  place_id          text,
  distance_m        integer,
  starts_at         timestamptz,
  ends_at           timestamptz,
  state             text,      -- now | soon | later_today | today (date known, time not)
  minutes_to_start  integer,
  minutes_left      integer,
  ends_on           date,
  days_left         integer,
  labels            text[],    -- evening, night, temporary, today_only, last_days, ends_soon,
                               -- starting_soon, free, venue_hours, date_only, end_estimated, multi_source
  confidence        real,
  is_free           boolean,
  price_text        text,
  url               text,
  sources           jsonb,
  description       text,
  lat               double precision,
  lon               double precision
)
language sql stable
as $$
with params as (
  select st_setsrid(st_makepoint(p_lon, p_lat), 4326)::geography              as g,
         (p_at at time zone p_tz)::date                                          as today_local,
         (((p_at at time zone p_tz)::date + 1)::timestamp at time zone p_tz)     as end_of_day
),
cand as (
  select e.id, e.title, e.kind, e.venue_name, e.place_id, e.starts_on, e.ends_on, e.is_temporary, e.is_free,
         e.price_text, e.url, e.sources, e.confidence, e.last_seen_at, e.description,
         st_y(e.geom::geometry) as e_lat, st_x(e.geom::geometry) as e_lon,
         s.starts_at as s_start, s.ends_at as s_end, s.time_known, s.end_estimated, s.hours_from,
         st_distance(e.geom, p.g) as dist,
         p.today_local
  from events e
  join event_sessions s on s.event_id = e.id
  cross join params p
  where e.status = 'scheduled'
    and e.geom is not null
    and st_dwithin(e.geom, p.g, p_radius_m)
    and s.ends_at > p_at
    and s.starts_at < greatest(p_at + p_horizon, p.end_of_day)
),
picked as (
  -- the session running now wins; otherwise the nearest upcoming one
  select distinct on (c.id) c.*
  from cand c
  order by c.id, (c.s_start <= p_at) desc, c.s_start
),
classified as (
  select pk.*,
    case
      when not pk.time_known          then 'today'
      when pk.s_start <= p_at         then 'now'
      when pk.s_start <= p_at + p_horizon then 'soon'
      else 'later_today'
    end as st,
    -- minutes since local midnight of the session's start day
    extract(hour from pk.s_start at time zone p_tz) * 60
      + extract(minute from pk.s_start at time zone p_tz)                       as start_min,
    extract(epoch from (pk.s_end
      - (date_trunc('day', pk.s_start at time zone p_tz) at time zone p_tz))) / 60 as end_min
  from picked pk
)
select
  c.id,
  c.title,
  c.kind,
  coalesce(pl.name, c.venue_name),
  c.place_id,
  round(c.dist)::int,
  c.s_start,
  c.s_end,
  c.st,
  case when c.st in ('soon', 'later_today') then ceil(extract(epoch from (c.s_start - p_at)) / 60)::int end,
  case when c.st = 'now' then floor(extract(epoch from (c.s_end - p_at)) / 60)::int end,
  c.ends_on,
  case when c.ends_on is not null then (c.ends_on - c.today_local) end,
  array_remove(array[
    case when c.time_known and c.start_min < 22 * 60
              and (c.start_min >= 17 * 60 or c.end_min >= 20 * 60)            then 'evening' end,
    case when c.time_known and (c.start_min >= 22 * 60 or c.end_min > 24 * 60 + 30) then 'night' end,
    case when c.is_temporary                                                   then 'temporary' end,
    case when c.kind in ('exhibition', 'festival', 'fair') and c.ends_on is null
              and c.starts_on = c.today_local                                  then 'today_only' end,
    case when c.ends_on is not null and c.ends_on - c.today_local between 0 and 7 then 'last_days' end,
    case when c.st = 'now' and c.s_end - p_at <= interval '45 minutes'          then 'ends_soon' end,
    case when c.st = 'soon' and c.s_start - p_at <= interval '60 minutes'       then 'starting_soon' end,
    case when c.is_free                                                        then 'free' end,
    case when c.hours_from = 'venue'                                           then 'venue_hours' end,
    case when not c.time_known                                                 then 'date_only' end,
    case when c.end_estimated                                                  then 'end_estimated' end,
    case when (select count(distinct split_part(split_part(s->>'url', '://', 2), '/', 1))
               from jsonb_array_elements(c.sources) s) > 1                     then 'multi_source' end
  ], null),
  least(0.98,
    c.confidence
    * case when p_at - c.last_seen_at > interval '7 days' then 0.7
           when p_at - c.last_seen_at > interval '2 days' then 0.85
           else 1 end
    * case when c.time_known then 1 else 0.85 end
  )::real,
  c.is_free,
  c.price_text,
  c.url,
  c.sources,
  c.description,
  c.e_lat,
  c.e_lon
from classified c
left join places pl on pl.id = c.place_id
order by
  case c.st when 'now' then 0 when 'soon' then 1 when 'later_today' then 2 else 3 end,
  c.dist,
  c.confidence desc;
$$;


-- Places near a point with their open state at p_at (from materialized place_hours).
create or replace function places_near(
  p_lat         double precision,
  p_lon         double precision,
  p_radius_m    double precision default 1000,
  p_at          timestamptz      default now(),
  p_categories  text[]           default null,
  p_limit       integer          default 200
) returns table (
  place_id          text,
  name              text,
  category          text,
  subcategory       text,
  indoor            text,
  distance_m        integer,
  open_state        text,      -- open | closed | unknown
  closes_at         timestamptz,
  opens_at          timestamptz,
  minutes_to_close  integer,
  minutes_to_open   integer,
  opening_hours     text,
  lat               double precision,
  lon               double precision,
  attrs             jsonb,
  aliases           text[],
  source_url        text,
  name_ru           text,
  name_kk           text,
  name_en           text
)
language sql stable
as $$
with pt as (select st_setsrid(st_makepoint(p_lon, p_lat), 4326)::geography as g),
base as (
  select p.*, st_distance(p.geom, pt.g) as dist,
         (p.hours_valid and p.hours_until is not null and p_at <= p.hours_until) as hours_known
  from places p cross join pt
  where st_dwithin(p.geom, pt.g, p_radius_m)
    and (p_categories is null or p.category = any (p_categories))
)
select
  b.id, b.name, b.category, b.subcategory, b.indoor, round(b.dist)::int,
  case when not b.hours_known then 'unknown'
       when cur.closes_at is not null then 'open'
       else 'closed' end,
  case when b.hours_known then cur.closes_at end,
  case when b.hours_known and cur.closes_at is null then nxt.opens_at end,
  case when b.hours_known and cur.closes_at is not null
       then floor(extract(epoch from (cur.closes_at - p_at)) / 60)::int end,
  case when b.hours_known and cur.closes_at is null and nxt.opens_at is not null
       then ceil(extract(epoch from (nxt.opens_at - p_at)) / 60)::int end,
  b.opening_hours,
  st_y(b.geom::geometry),
  st_x(b.geom::geometry),
  b.attrs,
  b.aliases,
  b.source_url,
  b.name_ru,
  b.name_kk,
  b.name_en
from base b
left join lateral (
  select h.closes_at from place_hours h
  where h.place_id = b.id and h.opens_at <= p_at and h.closes_at > p_at
  order by h.closes_at desc limit 1
) cur on true
left join lateral (
  select h.opens_at from place_hours h
  where h.place_id = b.id and h.opens_at > p_at
  order by h.opens_at limit 1
) nxt on true
order by b.dist
limit p_limit;
$$;
