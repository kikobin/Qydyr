from datetime import date, datetime, timezone

from qydyr_data.events.build import build_records, confidence
from qydyr_data.events.models import ExtractedEvent, RawEvent

TODAY = date(2026, 10, 20)
SEEN = datetime(2026, 10, 20, 3, tzinfo=timezone.utc)


def raw(event: dict, *, source_id="sxodim_afisha", url="https://sxodim.com/astana/event/noch-v-muzee",
        extraction="llm", mode="page", evidence_ok=True, consistent=True, venue_hint=None) -> RawEvent:
    return RawEvent(event=ExtractedEvent(**event), source_id=source_id, source_url=url, extraction=extraction,
                    mode=mode, reference_date=TODAY, fetched_at=SEEN, evidence_ok=evidence_ok,
                    dates_consistent=consistent, venue_hint=venue_hint)


NOCH = {"title": "Вечерняя выставка «Ночь в музее»", "kind": "exhibition",
        "venue_name": "Национальный музей Республики Казахстан", "date_start": "2026-10-23",
        "time_start": "18:00", "time_end": "22:00", "evidence": "Когда: 23 октября, с 18:00 до 22:00"}


def test_confidence_penalties():
    good = raw(NOCH)
    no_evidence = raw(NOCH, evidence_ok=False)
    contradicting = raw(NOCH, consistent=False)
    assert confidence(good, True) > confidence(no_evidence, True) > confidence(contradicting, True)
    assert confidence(raw(NOCH, extraction="jsonld", mode="jsonld"), True) > confidence(good, True)


def test_same_event_from_afisha_and_telegram_is_merged(sample_places_lite):
    a = raw(NOCH)
    b = raw({"title": "Ночь в музее", "kind": "exhibition", "date_start": "2026-10-23", "time_start": "18:00",
             "evidence": "23 октября в 18:00 — «Ночь в музее»"},
            source_id="telegram_afisha", url="https://t.me/test_afisha_channel/77", extraction="telegram", mode="post")
    recs = build_records([a, b], sample_places_lite, today=TODAY)
    assert len(recs) == 1
    r = recs[0]
    assert r.place_id == "osm:node/1001" and r.match_method == "exact"
    assert {s["source_id"] for s in r.sources} == {"sxodim_afisha", "telegram_afisha"}
    single = build_records([a], sample_places_lite, today=TODAY)[0]
    assert r.confidence > single.confidence  # two independent sites agree
    assert r.id == single.id                 # stable id: the located record wins


def test_past_and_dateless_events_are_dropped(sample_places_lite):
    past = raw({**NOCH, "title": "Прошедшая выставка", "date_start": "2026-10-01"})
    dateless = raw({"title": "Что-то без даты", "kind": "other"})
    assert build_records([past, dateless], sample_places_lite, today=TODAY) == []


def test_permanent_exhibition_with_schedule_only(sample_places_lite):
    perm = raw({"title": "Постоянная экспозиция «Золотой зал»", "kind": "exhibition",
                "venue_name": "Нацмузей", "schedule_opening_hours": "Tu-Su 10:00-18:00"})
    r = build_records([perm], sample_places_lite, today=TODAY, horizon_days=7)[0]
    assert not r.is_temporary
    assert r.starts_on is None and r.ends_on is None  # no invented "ends on" -> never "последние дни"
    assert len(r.sessions) == 7                      # Oct 20..27 minus Monday
