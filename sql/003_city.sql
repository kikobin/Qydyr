-- Qydyr for the city: what people look for and do not find, and where the city cannot answer typical requests.
-- Aggregates only: a ~1 km geohash cell (precision 6), the understood need, the day. No text, no coordinates,
-- no user, no time finer than a day.

-- Live demand: one row per day, cell and need, from the assistant's real requests.
create table if not exists demand (
  day     date not null,
  cell    text not null,                   -- geohash precision 6 (~1.2 x 0.6 km)
  need    text not null,                   -- kids, quiet, socket, accessible, night, ...
  asked   integer not null default 0,
  unmet   integer not null default 0,      -- no answer covered this need (yes / likely)
  primary key (day, cell, need),
  check (unmet <= asked)
);

-- Supply audit: the assistant asked the same typical requests in every populated cell.
create table if not exists city_audit (
  scenario     text not null,               -- kids_evening, work_quiet, step_free, night, frost, events
  cell         text not null,
  lat          double precision not null,
  lon          double precision not null,
  label        text,                        -- the nearest well-known place: "у Mega Silk Way"
  places       integer not null,            -- how many places the cell has (a proxy for how busy it is)
  options      integer not null,            -- answers that really fit the request (yes or likely)
  confirmed    integer not null default 0,  -- of them, fitting by the data itself (yes), not by a likely guess
  nearest_min  integer,                     -- minutes to the nearest fitting answer
  best         jsonb not null default '[]'::jsonb,   -- up to 2 titles
  at           timestamptz not null,        -- the moment the request was asked for
  run_at       timestamptz not null default now(),
  primary key (scenario, cell)
);
alter table city_audit add column if not exists confirmed integer not null default 0;
