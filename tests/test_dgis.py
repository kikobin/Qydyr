"""2ГИС Places API: schedules into OSM opening_hours, what is merged into a place and with which source, places
OSM lacks, the quota (cache, lookups) and how it shows in answers. The API answers here are synthetic, shaped
like the documented 3.0 /items format (tests/fixtures/dgis_items.json)."""
import json
from datetime import datetime

import httpx
import pytest

from qydyr_data import city, settings, store
from qydyr_data.assistant.recommend import Assistant, DbData
from qydyr_data.places import dgis
from qydyr_data.places.normalize import Place

from .conftest import FIX
from .test_pipeline_db import load_everything

ITEMS = json.loads((FIX / "dgis_items.json").read_text(encoding="utf-8"))


def day(fr, to):
    return {"working_hours": [{"from": fr, "to": to}]}


def test_schedule_to_opening_hours():
    week = {d: day("10:00", "22:00") for d in ("Mon", "Tue", "Wed", "Thu", "Fri")}
    assert dgis.schedule_to_oh({**week, "Sat": day("11:00", "23:00"), "Sun": day("11:00", "23:00")}) == \
        "Mo-Fr 10:00-22:00; Sa,Su 11:00-23:00"
    assert dgis.schedule_to_oh({"Tue": day("10:00", "18:00"), "Wed": day("10:00", "18:00")}) == "Tu,We 10:00-18:00"
    assert dgis.schedule_to_oh({"Fri": day("18:00", "02:00")}) == "Fr 18:00-02:00"            # past midnight
    assert dgis.schedule_to_oh({"Mon": day("08:00", "00:00")}) == "Mo 08:00-24:00"
    assert dgis.schedule_to_oh({"is_24x7": True}) == "24/7"
    assert dgis.schedule_to_oh({"Mon": {"working_hours": [{"from": "09:00", "to": "13:00"}, {"from": "14:00", "to": "18:00"}]}}) \
        == "Mo 09:00-13:00,14:00-18:00"
    assert dgis.schedule_to_oh({}) is None and dgis.schedule_to_oh({"is_24x7": False}) is None


def place(pid, name, lat, lon, **kw):
    return Place(id=pid, name=name, category=kw.pop("category", "culture"), subcategory=kw.pop("subcategory", "theatre"),
                 lat=lat, lon=lon, **kw)


def test_merge_fills_gaps_and_says_where_from():
    opera = place("osm:way/2002", "Astana Opera", 51.1307, 71.4146)
    dgis.merge(opera, ITEMS["Астана Опера"][0])
    assert opera.hours_valid and opera.opening_hours == "Tu-Su 10:00-20:00"
    assert opera.attrs["opening_hours:source"].startswith("https://2gis.kz/astana/firm/")
    assert (opera.attrs["2gis:rating"], opera.attrs["2gis:reviews"]) == (4.8, 2140)
    assert opera.attrs["wheelchair"] == "yes" and opera.attrs["wheelchair:source"] == "2gis"
    assert opera.phone == "+77172000001" and opera.website == "https://astanaopera.kz"
    # OpenStreetMap keeps the last word where it has a value
    cafe = place("osm:node/1005", "Кафе «Коктем»", 51.1192, 71.4689, category="food", subcategory="cafe",
                 opening_hours="Mo-Su 08:00-23:00", hours_valid=True, attrs={"wheelchair": "no"})
    dgis.merge(cafe, {**ITEMS["Кафе «Коктем»"][0], "attribute_groups": [{"name": "Доступность", "attributes": [{"name": "Пандус"}]},
                                                                        {"name": "Wi-Fi", "attributes": [{"name": "Wi-Fi"}]}]})
    assert cafe.opening_hours == "Mo-Su 08:00-23:00" and cafe.attrs["wheelchair"] == "no" and cafe.attrs["wifi"]


def test_same_place_needs_name_and_distance():
    opera = place("osm:way/2002", "Astana Opera", 51.1307, 71.4146, aliases=["Астана Опера"])
    item = ITEMS["Астана Опера"][0]
    assert dgis.same_place(opera, item) >= dgis.MATCH_NAME
    assert dgis.same_place(place("x", "Astana Opera", 51.2, 71.5), item) == 0             # 9 km away
    assert dgis.same_place(place("y", "Хан Шатыр", 51.1307, 71.4146), item) < dgis.MATCH_NAME
    assert dgis.category_for(ITEMS["кофейня"][2]) == ("kids", "kids_centre", "indoor")
    assert dgis.category_for(ITEMS["кофейня"][3]) is None                                  # a tyre shop is no outing


class FakeApi:
    """httpx transport answering like 2ГИС: items for known queries, 404 itemNotFound otherwise, 403 for a bad key."""
    def __init__(self):
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        q = request.url.params.get("q")
        self.calls.append(q)
        if request.url.params.get("key") == "bad":
            return httpx.Response(403, json={"meta": {"code": 403, "error": {"type": "keyIsBlocked", "message": "blocked"}}})
        items = ITEMS.get(q)
        if not items:
            return httpx.Response(200, json={"meta": {"code": 404, "error": {"type": "itemNotFound"}}})
        return httpx.Response(200, json={"meta": {"code": 200}, "result": {"items": items, "total": len(items)}})


def client(tmp_path, api, key="test"):
    return dgis.DgisClient(key, cache_dir=tmp_path, delay_s=0, client=httpx.Client(transport=httpx.MockTransport(api)))


def test_client_caches_and_reads_errors(tmp_path):
    api = FakeApi()
    c = client(tmp_path, api)
    assert len(c.search("кофейня", 71.41, 51.09, 700)) == 4 and c.requests == 1
    assert len(c.search("кофейня", 71.41, 51.09, 700)) == 4 and c.requests == 1             # from the disk cache
    assert c.search("нет такого", 71.41, 51.09, 700) == []                                  # 404 = nothing found
    with pytest.raises(RuntimeError, match="keyIsBlocked"):
        client(tmp_path / "x", FakeApi(), key="bad").search("кофейня", 71.41, 51.09, 700)
    assert "test" not in "".join(p.name for p in tmp_path.iterdir())                       # the key is not in the cache


def test_enrich_discover_apply(db, sample_places, tmp_path, monkeypatch):
    load_everything(db, sample_places)
    store.init_db(db)
    api = FakeApi()
    c = client(tmp_path, api)
    got = dgis.enrich(db, c, limit=100)
    assert got["matched"] == 2                                                              # the opera, the café
    assert dgis.enrich(db, c, limit=100)["asked"] == 0                                      # asked already: no quota spent
    monkeypatch.setattr(dgis, "DISCOVER", ["кофейня"])
    assert dgis.discover(db, c, limit=100)["new"] >= 3
    places = store.load_places(db)
    enriched, added = dgis.apply(db, places)
    by_id = {p.id: p for p in places}
    assert by_id["osm:way/2002"].hours_valid and by_id["osm:way/2010"].attrs["2gis:id"] == "70000001000000004"   # Mega linked, no twin
    assert {"2gis:70000001000000003", "2gis:70000001000000005"} <= set(by_id) and "2gis:70000001000000006" not in by_id
    boom = by_id["2gis:70000001000000003"]
    assert (boom.category, boom.subcategory, boom.source_id) == ("food", "cafe", "2gis")
    assert boom.opening_hours.startswith("Mo-Th 08:00-23:00; Fr 08:00-24:00; Sa 09:00-02:00")
    store.upsert_places(db, places)
    src = db.execute("select kind, last_status from sources where id = '2gis'").fetchone()
    assert src["kind"] == "catalog" and "new places" in src["last_status"]
    assert any(s["id"] == "2gis" for s in city.data_view(db)["sources"])

    # in answers: a rating as a reason, 2ГИС as a source, sockets from 2ГИС
    at = datetime(2026, 10, 23, 15, 0, tzinfo=settings.TZ)
    ans = Assistant(DbData(db)).ask("тихо поработать: кофе и розетка", lat=51.0905, lon=71.4150, at=at,
                                    weather_file=FIX / "weather_mild.json")
    item = next(i for i in ans.items if i.title == "Coffee Boom")
    assert any("рейтинг 4,7 в 2ГИС, 320 отзывов" in w for w in item.why)
    assert item.need_hits["socket"] == "yes" and any("2gis.kz" in s for s in item.sources)
    assert item.where == "БЦ Изумрудный квартал, 1 этаж"                                  # where inside, from 2ГИС


# ------------------------------------------------------------------ rubric import and the tags 2ГИС really returns
def item(iid, name, lat, lon, rubric=("162", "Кофейни"), tags=(), **extra):
    """An item shaped like the real 3.0 answer (seen with dgis-probe): rubric ids, attribute tags, address."""
    groups = {}
    for group, name_, tag in tags:
        groups.setdefault(group, []).append({"name": name_, "tag": tag})
    return {"id": iid, "type": "branch", "name": name, "name_ex": {"primary": name}, "point": {"lat": lat, "lon": lon},
            "rubrics": [{"id": rubric[0], "name": rubric[1], "kind": "primary"}],
            "attribute_groups": [{"name": g, "attributes": a} for g, a in groups.items()], **extra}


def test_merge_reads_the_tags():
    it = item("70000001000000101", "Тихая чашка", 51.1, 71.4, tags=[
        ("Ресторан / Кафе", "Чек 3 000 ₸", "food_service_avg_price"),
        ("Ресторан / Кафе", "Можно с ноутбуком", "food_service_cafe_working"),
        ("Ресторан / Кафе", "Детское меню", "food_service_details_food_kids_menu"),
        ("Ресторан / Кафе", "Меню на английском", "food_service_details_food_english_menu"),
        ("Ресторан / Кафе", "До 45 мест", "food_service_capacity"),
        ("Доступная среда", "Пандус", "accessible_entrance_inclusive_environment_ramp_inclusive"),
        ("Доступная среда", "Широкий лифт", "accessible_entrance_inclusive_environment_elevator_inclusive")],
        address={"building_name": "ТРЦ Керуен"}, address_comment="3 этаж",
        schedule={"Mon": day("10:00", "22:00"), "comment": "кухня до 21:30"},
        links={"nearest_stations": [{"name": "Керуен", "distance": 420, "route_types": ["bus"]},
                                    {"name": "Дальняя", "distance": 900, "route_types": ["bus"]}]})
    p = place("osm:node/9", "Тихая чашка", 51.1, 71.4, category="food", subcategory="cafe", indoor="unknown")
    dgis.merge(p, it)
    a = p.attrs
    assert a["avg_check"] == 3000 and a["capacity_text"] == "До 45 мест"
    assert (a["laptop"], a["kids_friendly"], a["english_menu"]) == ("yes", "kids_menu", "yes")
    assert a["wheelchair"] == "yes" and a["access:features"] == ["Пандус", "Широкий лифт"]
    assert (a["in_building"], a["where_inside"], p.indoor) == ("ТРЦ Керуен", "3 этаж", "indoor")
    assert a["hours_note"] == "кухня до 21:30" and a["nearest_station"] == {"name": "Керуен", "m": 420, "types": ["bus"]}
    # a mall is not "in" itself
    mall = place("osm:way/7", "ТРЦ Керуен", 51.1, 71.4, category="shopping", subcategory="mall")
    dgis.merge(mall, item("70000001000000102", "ТРЦ Керуен", 51.1, 71.4, rubric=("19499", "ТРЦ"),
                          address={"building_name": "ТРЦ Керуен"}))
    assert "in_building" not in mall.attrs


def test_to_place_by_rubric_id():
    kids = dgis.to_place(item("1", "Мадагаскар", 51.1, 71.4, rubric=("16743", "Детские игровые залы")))
    assert (kids.category, kids.subcategory, kids.source_id) == ("kids", "kids_centre", "2gis")
    # the primary rubric is not ours, an additional one is: still a coffee shop
    both = item("2", "Кофе и доставка", 51.1, 71.4, rubric=("1203", "Доставка еды"))
    both["rubrics"].append({"id": "162", "name": "Кофейни", "kind": "additional"})
    assert dgis.to_place(both).subcategory == "cafe"
    assert dgis.to_place(item("3", "Шиномонтаж", 51.1, 71.4, rubric=("405", "Шиномонтаж"))) is None


class RubricApi:
    """2ГИС answering rubric_id queries: a city-wide total, at most 10 per page and 5 pages, like the demo key."""
    def __init__(self, city_total: dict[str, int], per_cell: int = 12):
        self.city_total, self.per_cell, self.calls = city_total, per_cell, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        q = request.url.params
        rid, page, size = q["rubric_id"], int(q["page"]), int(q["page_size"])
        assert size <= 10 and page <= 5, "the key refuses bigger pages"
        self.calls.append((rid, q.get("point"), page))
        total = self.per_cell if q.get("point") else self.city_total.get(rid, 0)
        lon, lat = map(float, q["point"].split(",")) if q.get("point") else (71.43, 51.13)
        tag = (q.get("point") or "city").replace(",", "")
        items = [item(f"7{rid}{tag}{k}".replace(".", ""), f"{rid} №{k}", lat + k * 1e-4, lon, rubric=(rid, "x"))
                 for k in range((page - 1) * size, min(total, page * size))]
        if not items:
            return httpx.Response(200, json={"meta": {"code": 404, "error": {"type": "itemNotFound"}}})
        return httpx.Response(200, json={"meta": {"code": 200}, "result": {"items": items, "total": total}})


def test_import_rubrics_pages_splits_and_resumes(db, sample_places, tmp_path, monkeypatch):
    load_everything(db, sample_places)
    store.init_db(db)
    monkeypatch.setattr(dgis, "RUBRIC_IMPORT", [(1, "16743", ("kids", "kids_centre", "indoor")),
                                                (2, "162", ("food", "cafe", "indoor"))])
    api = RubricApi({"16743": 23, "162": 75})
    c = client(tmp_path, api)
    got = dgis.import_rubrics(db, c, limit=1000)
    assert got["16743"] == 23 and [p for r, pt, p in api.calls if r == "16743"] == [1, 2, 3]   # 3 pages, city-wide
    cells = {pt for r, pt, _ in api.calls if r == "162" and pt}
    assert cells and got["162"] == 10 + 12 * len(cells)      # 75 > 50: asked district by district instead
    stored = db.execute("select count(*) n from dgis_items").fetchone()["n"]
    assert stored == got["16743"] + got["162"]
    # a second run spends nothing: every page is in dgis_lookups
    c2 = client(tmp_path / "again", RubricApi({"16743": 23, "162": 75}))
    assert dgis.import_rubrics(db, c2, limit=1000) == {} and c2.requests == 0
    # the quota stops a run cleanly, and the next one continues
    db.execute("delete from dgis_lookups")
    c3 = client(tmp_path / "third", RubricApi({"16743": 23, "162": 75}))
    dgis.import_rubrics(db, c3, limit=2)
    assert c3.requests == 2
    places = store.load_places(db)
    _, added = dgis.apply(db, places)
    assert added == stored and sum(p.subcategory == "kids_centre" and p.source_id == "2gis" for p in places) == 23


def test_needs_read_2gis_attrs():
    from qydyr_data.assistant import knowledge as K
    from qydyr_data.assistant.intent import parse_rules

    def cafe(**attrs):
        return K.Cand(kind="place", id="c", title="c", lat=0, lon=0, category="food", subcategory="cafe", attrs=attrs)
    assert K.need_match(cafe(avg_check=2500), "cheap") == ("yes", "средний чек 2 500 ₸ (2ГИС)")
    assert K.need_match(cafe(avg_check=5000), "cheap")[0] == "likely"
    assert K.need_match(cafe(avg_check=12000), "cheap")[0] == "no"
    assert K.need_match(cafe(laptop="yes"), "socket") == ("yes", "можно с ноутбуком (2ГИС)")
    assert K.need_match(cafe(kids_friendly="kids_area"), "kids")[0] == "yes"
    assert K.need_match(cafe(kids_friendly="kids_menu"), "kids")[0] == "likely"
    assert K.need_match(cafe(wheelchair="yes", **{"access:features": ["Пандус", "Широкий лифт"]}), "accessible") \
        == ("yes", "пандус, широкий лифт (2ГИС)")
    assert K.need_match(cafe(wifi=True, **{"wifi:source": "2gis"}), "wifi") == ("yes", "Wi‑Fi (2ГИС)")
    assert "cheap" in parse_rules("где недорого поесть у Байтерека").needs
    assert "cheap" in parse_rules("арзан кафе").needs and "cheap" not in parse_rules("кафе с видом").needs
