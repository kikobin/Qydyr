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
    assert (item.rating, item.reviews) == (4.7, 320)
    from qydyr_data.bot.fmt import answer_html
    assert "★ 4,7 (2ГИС)" in answer_html(ans.model_dump(mode="json"))


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


# ------------------------------------------------------------------ typed addresses as the start point
def test_address_part():
    a = dgis.address_part
    assert a("Кенесары 40 с ребёнком") == "Кенесары 40" and a("улице Сыганак 10/1") == "улице Сыганак 10/1"
    assert a("проспекте Мангилик Ел 55, хочу кофе") == "проспекте Мангилик Ел 55" and a("Кенесары 40к") == "Кенесары 40к"
    assert a("Туран 37 в 18:00") == "Туран 37"
    # a name with a year, a count, a floor, a duration: not addresses
    for text in ("Экспо 2017", "кофе на 2 часа", "Хан Шатыр 2 этаж", "Байтерека через 15 минут", "кафе 24 часа",
                 "Mega Silk Way", "Хан Шатыре на 2 часа с 3 детьми"):
        assert a(text) is None, text


class GeocodeApi:
    """The 2ГИС geocoder as it really answers: buildings, sometimes in another town or with another number."""
    ANSWERS = {
        "Кенесары 40": [{"type": "building", "name": "7 Континент, бизнес-центр", "address_name": "улица Кенесары, 40",
                         "full_address_name": "Астана, улица Кенесары, 40", "point": {"lat": 51.16561, "lon": 71.432613}}],
        "Туран 37": [{"type": "building", "name": "Хан Шатыр, торгово-развлекательный центр",
                      "address_name": "проспект Туран, 37", "point": {"lat": 51.1325, "lon": 71.4036}}],
        "Республики 20": [{"type": "street", "name": "проспект Республики", "point": {"lat": 51.16, "lon": 71.43}},
                          {"type": "building", "name": "проспект Республики, 18", "address_name": "проспект Республики, 18",
                           "point": {"lat": 51.16, "lon": 71.43}}],
        "Абай проспект 12": [{"type": "building", "name": "улица Абай Кунанбайулы, 12", "address_name": "улица Абай Кунанбайулы, 12",
                              "full_address_name": "Ыбырая Алтынсарина, улица Абай Кунанбайулы, 12",
                              "point": {"lat": 51.245, "lon": 71.265}}],
    }

    def __init__(self):
        self.asked = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        q = request.url.params["q"].removeprefix("Астана, ")
        self.asked.append(q)
        items = self.ANSWERS.get(q)
        if not items:
            return httpx.Response(200, json={"meta": {"code": 404}})
        return httpx.Response(200, json={"meta": {"code": 200}, "result": {"items": items, "total": len(items)}})


def test_geocode_checks_the_answer(tmp_path):
    api = GeocodeApi()
    g = dgis.Geocoder(client(tmp_path, api))
    assert g("Кенесары 40") == {"lat": 51.16561, "lon": 71.432613, "label": "улица Кенесары, 40 · 7 Континент"}
    assert g("Туран 37")["label"] == "проспект Туран, 37 · Хан Шатыр"
    assert g("Республики 20") is None                            # the street, or house 18: not the 20 asked for
    assert g("Абай даңғылы 12") is None and api.asked[-1] == "Абай проспект 12"   # asked in Russian; a village street
    assert g("Хан Шатыр") is None and g("кофе на 2 часа") is None and len(api.asked) == 4   # not addresses: not asked
    # 2ГИС down: one failure, then it rests instead of slowing every answer
    def down(request):
        raise httpx.ConnectTimeout("handshake")
    broken = dgis.Geocoder(client(tmp_path / "down", down))
    assert broken("Кенесары 40") is None and broken._rest_until > 0


def test_assistant_starts_from_an_address(db, sample_places):
    load_everything(db, sample_places)
    asked = []

    def geocode(text):
        asked.append(text)
        return {"lat": 51.1286, "lon": 71.4307, "label": "улица Кенесары, 40"} if "Кенесары 40" in text else None
    at = datetime(2026, 10, 23, 15, 0, tzinfo=settings.TZ)
    a = Assistant(DbData(db), geocode=geocode)
    ans = a.ask("я на Кенесары 40, хочу кофе", at=at, weather_file=FIX / "weather_mild.json")
    assert ans.anchor.source == "address" and ans.anchor.label == "улица Кенесары, 40"
    assert (ans.anchor.lat, ans.anchor.lon) != (51.1286, 71.4307)               # rounded like GPS, not kept exact
    assert any("адрес найден в 2ГИС" in u for u in ans.used_data) and ans.items
    # a place's whole name wins without asking 2ГИС; a destination is not where the person is
    asked.clear()
    ans = a.ask("я у Хан Шатыра, хочу кофе", at=at, weather_file=FIX / "weather_mild.json")
    assert ans.anchor.source == "place" and asked == []
    ans = a.ask("хочу на Кенесары 40", lat=51.09, lon=71.41, at=at, weather_file=FIX / "weather_mild.json")
    assert ans.anchor.source == "gps"


def test_export_and_load_elsewhere(db, sample_places, tmp_path):
    """Production gets the answers from a file: no key, no requests; the bulky rubric id lists are dropped."""
    load_everything(db, sample_places)
    store.init_db(db)
    noisy = item("70000001000000201", "Кофе Тест", 51.09, 71.41, tags=[("Wi-Fi", "Wi-Fi", "wifi")])
    noisy["attribute_groups"][0]["rubric_ids"] = [str(i) for i in range(500)]
    dgis._save(db, noisy, None, "rubric:162:city:1")
    dgis._save(db, ITEMS["Астана Опера"][0], "osm:way/2002", "place:osm:way/2002")
    dgis._remember(db, "rubric:162:city:1", 1)
    f = tmp_path / "dgis.jsonl.gz"
    assert dgis.export_items(db, f) == 2
    db.execute("delete from dgis_items; delete from dgis_lookups")
    assert dgis.import_items(db, f) == 2
    assert dgis._asked(db, "rubric:162:city:1", 30)            # asked there: not asked again here
    row = db.execute("select data, place_id from dgis_items where id = '70000001000000201'").fetchone()
    assert "rubric_ids" not in row["data"]["attribute_groups"][0] and row["place_id"] is None
    places = store.load_places(db)
    enriched, added = dgis.apply(db, places)
    assert (enriched, added) == (1, 1)


def test_unnamed_attraction_is_named_by_what_it_is():
    it = {"id": "70030076193065735", "type": "attraction", "full_name": "Астана, Смотровая площадка",
          "point": {"lat": 51.12, "lon": 71.43}, "rubrics": [{"id": "111526", "name": "Смотровые площадки", "kind": "primary"}]}
    assert dgis.to_place(it).name == "Смотровая площадка"


def test_more_tags_view_dog_music_lunch_cuisine():
    from qydyr_data.assistant import knowledge as K
    from qydyr_data.assistant.intent import parse_rules
    it = item("70000001000000301", "Высота", 51.1, 71.4, rubric=("164", "Рестораны"), tags=[
        ("Ресторан / Кафе", "Панорамный вид", "food_service_infrastructure_panoramic_restaurant"),
        ("Ресторан / Кафе", "Живая музыка", "food_service_details_food_live_music"),
        ("Ресторан / Кафе", "Бизнес-ланч", "food_service_business_lunch"),
        ("Ресторан / Кафе", "Можно с собакой", "food_service_dog_friendly_food"),
        ("Ресторан / Кафе", "Столики на улице", "food_service_infrastructure_outdoor_tables_eat_out"),
        ("Ресторан / Кафе", "Чек 12000 ₸", "food_service_avg_price"),
        ("Кухня", "Европейская кухня", "food_service_food_european")])
    p = dgis.to_place(it)
    a = p.attrs
    assert (a["view"], a["view:source"], a["dog"], a["live_music"], a["business_lunch"], a["outdoor_seating"]) == \
        ("yes", "2gis", "yes", "yes", "yes", "yes")
    c = K.Cand(kind="place", id=p.id, title=p.name, lat=p.lat, lon=p.lon, category=p.category,
               subcategory=p.subcategory, indoor=p.indoor, attrs=a)
    assert K.need_match(c, "view") == ("yes", "панорамный вид (2ГИС)")
    assert K.need_match(c, "romantic")[0] == "yes" and K.need_match(c, "dog") == ("yes", "можно с собакой (2ГИС)")
    tue_lunch, sat = datetime(2026, 10, 20, 13, 0, tzinfo=settings.TZ), datetime(2026, 10, 24, 13, 0, tzinfo=settings.TZ)
    assert K.need_match(c, "cheap", at=tue_lunch)[0] == "likely"     # dear, but its weekday lunch is not
    assert K.need_match(c, "cheap", at=sat)[0] == "no"
    assert K.idea(c) == ("попробуйте блюда европейской кухни", "idea")
    osm = K.Cand(kind="place", id="o", title="o", lat=0, lon=0, category="food", subcategory="restaurant",
                 attrs={"cuisine": "uzbek;kazakh"})
    assert K.idea(osm)[0] == "попробуйте блюда узбекской кухни"
    museum = K.Cand(kind="place", id="m", title="m", lat=0, lon=0, category="culture", subcategory="museum", indoor="indoor")
    assert K.need_match(museum, "dog")[0] == "unknown"            # no data, no guess
    assert "dog" in parse_rules("куда сходить с собакой").needs and "dog" in parse_rules("итпен серуендеу").needs


def test_place_questions_use_2gis_facts():
    from qydyr_data.assistant import placeqa
    row = {"place_id": "2gis:1", "closes_at": datetime(2026, 10, 21, 0, 0, tzinfo=settings.TZ), "opens_at": None,
           "name": "La Bottega", "category": "food", "subcategory": "restaurant", "indoor": "indoor",
           "open_state": "open", "opening_hours": "Mo-Su 10:00-24:00", "lat": 51.128, "lon": 71.425,
           "attrs": {"avg_check": 7000, "business_lunch": "yes", "in_building": "ТРЦ Керуен", "where_inside": "3 этаж",
                     "2gis:rating": 4.9, "2gis:reviews": 652, "2gis:url": "https://2gis.kz/astana/firm/1",
                     "nearest_station": {"name": "Керуен", "m": 120, "types": ["bus"]},
                     "opening_hours:source": "https://2gis.kz/astana/firm/1", "hours_note": "кухня до 23:00"}}
    at = datetime(2026, 10, 20, 13, 0, tzinfo=settings.TZ)
    assert placeqa.answer(row, [], "какой тут средний чек?", at).answer == "Средний чек около 7 000 ₸ (2ГИС). Есть бизнес-ланч."
    where = placeqa.answer(row, [], "на каком этаже?", at)
    assert where.answer == "Где: ТРЦ Керуен, 3 этаж. Рядом остановка «Керуен», 120 м." and where.certainty == "yes"
    assert "4,9 — 652 отзывов" in placeqa.answer(row, [], "какие отзывы?", at).answer
    hours = placeqa.answer(row, [], "до скольки работает?", at)
    assert "Кухня до 23:00." in hours.answer and hours.evidence[0].startswith("2ГИС")
