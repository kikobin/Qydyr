"""Photo instead of an address (EXIF stripped, Claude vision faked) and the spoken story of a place."""
import io
from datetime import datetime

from PIL import Image

from qydyr_data import settings, store
from qydyr_data.api import run_whereami
from qydyr_data.assistant.recommend import DbData
from qydyr_data.assistant.story import tell
from qydyr_data.assistant.whereami import Landmark, clean_image

from .test_pipeline_db import load_everything

TZ = settings.TZ


def photo_with_gps() -> bytes:
    img = Image.new("RGB", (64, 48), (30, 120, 200))
    exif = Image.Exif()
    exif[0x8825] = {2: (51.0, 7.0, 42.0), 4: (71.0, 25.0, 50.0)}   # GPSInfo: the exact spot
    exif[0x010F] = "PhoneMaker"
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


class FakeVision:
    def __init__(self, lm):
        self.lm, self.calls = lm, []

    def structured(self, *, system, user, schema, max_tokens):
        self.calls.append(user)
        return self.lm


def test_clean_image_drops_exif():
    raw = photo_with_gps()
    assert Image.open(io.BytesIO(raw)).getexif()
    clean = clean_image(raw)
    assert not Image.open(io.BytesIO(clean)).getexif()


def test_whereami_finds_landmark_by_local_name(db, sample_places, monkeypatch):
    load_everything(db, sample_places)
    monkeypatch.setattr(settings, "DEMO_NOW", "2026-10-23T15:00")
    monkeypatch.setattr(settings, "WEATHER_FILE", str(settings.ROOT / "tests/fixtures/weather_mild.json"))
    vision = FakeVision(Landmark(in_astana=True, landmark="Чупа-чупс", alternatives=["Байтерек"], confidence="high",
                                 seen="башня с золотым шаром"))
    res = run_whereami(photo_with_gps(), DbData(db), llm=vision, text="что посмотреть рядом")
    assert res["found"] and res["place"] == "Байтерек"
    assert res["answer"]["anchor"]["source"] == "photo" and res["answer"]["items"]
    assert "по фото, само фото не сохраняется" in " ".join(res["answer"]["used_data"])
    sent = vision.calls[0][0]["source"]["data"]
    import base64
    assert not Image.open(io.BytesIO(base64.b64decode(sent))).getexif()   # what went to Claude has no GPS
    unsure = FakeVision(Landmark(in_astana=True, landmark="что-то", alternatives=[], confidence="low", seen="стена"))
    assert run_whereami(photo_with_gps(), DbData(db), llm=unsure)["found"] is False


def test_story_from_facts_and_from_claude(db, sample_places):
    load_everything(db, sample_places)
    at = datetime(2026, 10, 23, 19, 10, tzinfo=TZ)
    museum = next(p for p in sample_places if p.name.startswith("Национальный музей"))
    row = store.place(db, museum.id, at)
    evs = [e for e in store.whats_on(db, row["lat"], row["lon"], 150, at, 180) if e["place_id"] == museum.id]
    plain = tell(row, evs, "ru")
    assert plain["source"] == "facts" and "музей" in plain["text"] and "Ночь в музее" in plain["text"]

    class FakeText:
        def text(self, *, system, user, max_tokens):
            assert "ТОЛЬКО факты" in system and "FACTS" in user
            return "Вы у Национального музея: сегодня вечером открыты залы золота."
    told = tell(row, evs, "kk", llm=FakeText())
    assert told["source"] == "claude" and told["text"].startswith("Вы у Национального музея")
