-- 2ГИС Places API: what OpenStreetMap lacks (hours, ratings, Wi-Fi, step-free entrances, phones) and the places
-- it has not mapped. Kept apart from `places`, merged into them on every `places` / `dgis` run.

alter table sources drop constraint if exists sources_kind_check;
alter table sources add constraint sources_kind_check
  check (kind in ('osm', 'listing', 'article_index', 'page', 'telegram', 'manual', 'catalog'));

create table if not exists dgis_items (
  id          text primary key,                  -- 2GIS branch id
  place_id    text,                              -- the OSM place it is (null: a place OSM does not have)
  name        text not null,
  lat         double precision not null,
  lon         double precision not null,
  data        jsonb not null,                    -- the item as the API returned it
  query       text,                              -- what found it
  fetched_at  timestamptz not null default now()
);
create index if not exists dgis_items_place_idx on dgis_items (place_id);

-- What was already asked, so a run continues where the last one stopped (the API key has a quota).
create table if not exists dgis_lookups (
  key       text primary key,                    -- 'place:<id>' or 'area:<cell>:<query>'
  asked_at  timestamptz not null default now(),
  found     integer not null default 0
);
