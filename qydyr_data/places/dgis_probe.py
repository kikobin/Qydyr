"""Probe what the 2ГИС key can do, and how the answers look, before building on it.

    python -m qydyr_data dgis-probe          # -> data/dgis_probe/*.json + data/dgis_probe.zip

One small request to each API we may use (places with every field, by id, geocoder, suggest, rubrics, walking
routes, distance matrix, isochrones, public transport, reviews). Each answer is saved as is, with the request
(without the key) and the HTTP status, so field names and shapes come from the real API, not from memory.
A wrong guess is still useful: 2ГИС answers with an error that names what it did not accept."""
from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path

import httpx

from .. import settings

BAITEREK = (51.1283, 71.4305)       # lat, lon
KHAN_SHATYR = (51.1325, 71.4038)
MEGA = (51.0897, 71.4078)
ALL_FIELDS = ",".join(f"items.{f}" for f in (
    "point", "schedule", "reviews", "rubrics", "attribute_groups", "contact_groups", "name_ex", "address", "org",
    "external_content", "flags", "links", "floors", "context", "description", "ads", "dates", "adm_div",
    "full_address_name", "caption", "purpose", "structure_info", "group", "itin", "employees_org_count", "stat",
    "is_paying", "has_goods", "has_photos"))
SAFE_FIELDS = "items.point,items.schedule,items.reviews,items.rubrics,items.attribute_groups,items.contact_groups,items.name_ex"


def _probes(key: str, first_id: str | None) -> list[tuple[str, str, str, dict | None]]:
    lat, lon = BAITEREK
    pt = f"{lon},{lat}"
    out = [
        ("places_all_fields", "GET", "https://catalog.api.2gis.com/3.0/items",
         {"q": "кофейня", "point": pt, "radius": 1000, "page_size": 3, "fields": ALL_FIELDS, "locale": "ru_KZ"}),
        ("places_safe_fields", "GET", "https://catalog.api.2gis.com/3.0/items",
         {"q": "кофейня", "point": pt, "radius": 1000, "page_size": 3, "fields": SAFE_FIELDS, "locale": "ru_KZ"}),
        ("places_kids", "GET", "https://catalog.api.2gis.com/3.0/items",
         {"q": "детский развлекательный центр", "point": pt, "radius": 3000, "page_size": 3, "fields": SAFE_FIELDS}),
        ("places_mall_floors", "GET", "https://catalog.api.2gis.com/3.0/items",
         {"q": "Хан Шатыр", "point": pt, "radius": 3000, "page_size": 3, "fields": ALL_FIELDS}),
        ("places_attraction", "GET", "https://catalog.api.2gis.com/3.0/items",
         {"q": "Байтерек", "point": pt, "radius": 2000, "page_size": 3, "fields": ALL_FIELDS}),
        ("places_paging_total", "GET", "https://catalog.api.2gis.com/3.0/items",
         {"q": "кафе", "point": pt, "radius": 2000, "page_size": 10, "page": 2, "fields": "items.point"}),
        ("geocode_address", "GET", "https://catalog.api.2gis.com/3.0/items/geocode",
         {"q": "Астана, проспект Кабанбай батыра, 62", "fields": "items.point,items.full_address_name"}),
        ("geocode_landmark", "GET", "https://catalog.api.2gis.com/3.0/items/geocode",
         {"q": "Астана, ЦУМ", "fields": "items.point"}),
        ("suggest", "GET", "https://catalog.api.2gis.com/3.0/suggests",
         {"q": "хан шат", "location": pt, "fields": "items.point,items.rubrics", "locale": "ru_KZ"}),
        ("region_search", "GET", "https://catalog.api.2gis.com/2.0/region/search", {"q": "Астана"}),
        ("rubric_search", "GET", "https://catalog.api.2gis.com/2.0/catalog/rubric/search", {"q": "кофейни", "region_id": "68"}),
        ("routing_walking", "POST", "https://routing.api.2gis.com/routing/7.0.0/global",
         {"points": [{"type": "stop", "lat": BAITEREK[0], "lon": BAITEREK[1]},
                     {"type": "stop", "lat": KHAN_SHATYR[0], "lon": KHAN_SHATYR[1]}],
          "transport": "walking", "output": "detailed", "locale": "ru"}),
        ("distance_matrix_walking", "POST", "https://routing.api.2gis.com/get_dist_matrix",
         {"points": [{"lat": BAITEREK[0], "lon": BAITEREK[1]}, {"lat": KHAN_SHATYR[0], "lon": KHAN_SHATYR[1]},
                     {"lat": MEGA[0], "lon": MEGA[1]}],
          "sources": [0], "targets": [1, 2], "transport": "walking"}),
        ("isochrone_walking", "POST", "https://routing.api.2gis.com/isochrone/2.0.0",
         {"start": {"lat": BAITEREK[0], "lon": BAITEREK[1]}, "durations": [600, 1200], "transport": "walking"}),
        ("public_transport", "POST", "https://routing.api.2gis.com/public_transport/2.0",
         {"source": {"name": "Байтерек", "point": {"lat": BAITEREK[0], "lon": BAITEREK[1]}},
          "target": {"name": "Mega", "point": {"lat": MEGA[0], "lon": MEGA[1]}},
          "transport": ["bus", "trolleybus", "light_rail", "shuttle_bus"], "locale": "ru"}),
    ]
    if first_id:
        out += [
            ("places_byid", "GET", "https://catalog.api.2gis.com/3.0/items/byid", {"id": first_id, "fields": ALL_FIELDS}),
            ("reviews", "GET", f"https://public-api.reviews.2gis.com/2.0/branches/{first_id.split('_')[0]}/reviews",
             {"limit": 5, "locale": "ru_KZ", "fields": "meta.branch_rating,meta.branch_reviews_count"}),
        ]
    return out


def run(key: str | None = None, out_dir: Path | None = None, client: httpx.Client | None = None) -> Path:
    key = key or settings.DGIS_API_KEY
    if not key:
        raise RuntimeError("QYDYR_DGIS_KEY is not set")
    out_dir = out_dir or settings.DATA_DIR / "dgis_probe"
    out_dir.mkdir(parents=True, exist_ok=True)
    client = client or httpx.Client(timeout=20, headers={"User-Agent": settings.USER_AGENT})
    first_id = None
    summary = []
    probes = _probes(key, None)
    k = 0
    while k < len(probes):
        name, method, url, payload = probes[k]
        k += 1
        rec = {"name": name, "method": method, "url": url, "params_or_body": payload}
        try:
            if method == "GET":
                r = client.get(url, params={**payload, "key": key})
            else:
                r = client.post(url, params={"key": key, **({"version": "2.0"} if "dist_matrix" in url else {})}, json=payload)
            rec["status"] = r.status_code
            try:
                body = r.json()
            except ValueError:
                body = r.text[:20000]
            rec["body"] = body
            if name == "places_safe_fields" and first_id is None and isinstance(body, dict):
                items = (body.get("result") or {}).get("items") or []
                first_id = items[0]["id"] if items else None
                probes += [p for p in _probes(key, first_id) if p[0] in ("places_byid", "reviews")]
        except httpx.HTTPError as e:
            rec["error"] = f"{type(e).__name__}: {e}"
        text = json.dumps(rec, ensure_ascii=False, indent=1).replace(key, "<key>")
        (out_dir / f"{k:02d}_{name}.json").write_text(text, encoding="utf-8")
        meta = (rec.get("body") or {}).get("meta", {}) if isinstance(rec.get("body"), dict) else {}
        summary.append(f"{k:02d} {name}: HTTP {rec.get('status', '-')} {meta.get('code', '')} "
                       f"{(meta.get('error') or {}).get('message', '') or rec.get('error', '')}".strip())
        time.sleep(0.3)
    (out_dir / "summary.txt").write_text("\n".join(summary) + "\n", encoding="utf-8")
    zpath = out_dir.with_suffix(".zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(out_dir.iterdir()):
            z.write(f, f.name)
    print("\n".join(summary))
    return zpath
