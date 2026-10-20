"""Dates, names, opening hours, JSON-LD, page text, Telegram."""
from datetime import date, datetime, time, timezone

from qydyr_data import hours, settings
from qydyr_data.events import jsonld, telegram
from qydyr_data.events.llm import evidence_found
from qydyr_data.events.pagetext import find_links, main_text
from qydyr_data.events.ru_dates import dates_consistent, find_dates, find_ranges, find_times
from qydyr_data.textnorm import match_key, name_keys

from .conftest import fixture_text

REF = date(2026, 10, 20)


# ---------- dates ----------
def test_ranges_and_year_inference():
    assert find_ranges("с 10 октября по 30 ноября", REF) == [(date(2026, 10, 10), date(2026, 11, 30))]
    assert find_ranges("21–23 октября", REF) == [(date(2026, 10, 21), date(2026, 10, 23))]
    assert find_ranges("20 декабря – 10 января", REF) == [(date(2026, 12, 20), date(2027, 1, 10))]
    assert find_ranges("20 августа - 14 сентября", REF) == [(date(2026, 8, 20), date(2026, 9, 14))]
    assert find_ranges("до 30 ноября", REF) == [(None, date(2026, 11, 30))]


def test_single_dates_kazakh_and_numeric():
    assert find_dates("25 октября в 19:00", REF) == [date(2026, 10, 25)]
    assert find_dates("21-қазан сағат 19:00", REF) == [date(2026, 10, 21)]
    assert find_dates("21.10.2026, начало в 18.30", REF) == [date(2026, 10, 21)]
    assert find_times("21.10.2026, начало в 18.30") == [time(18, 30)]
    assert find_times("с 10.00 до 18.00 ежедневно") == [time(10, 0), time(18, 0)]


def test_dates_consistent():
    assert dates_consistent([date(2026, 10, 25)], "25 октября в 19:00", REF) is True
    assert dates_consistent([date(2026, 10, 26)], "25 октября в 19:00", REF) is False
    assert dates_consistent([date(2026, 11, 5)], "с 10 октября по 30 ноября", REF) is True
    assert dates_consistent([date(2026, 10, 25)], "сегодня вечером", REF) is None


# ---------- names ----------
def test_name_keys_cross_script():
    assert match_key("Хан-Шатыр") == match_key("Khan Shatyr") == match_key("ТРЦ «Хан Шатыр»")
    assert name_keys("Астана Опера") & name_keys("Astana Opera")
    assert match_key("Байтерек") == match_key("Bayterek")


# ---------- opening hours ----------
def test_last_tuesday_off_and_overnight():
    tz = settings.TZ
    museum = "Tu-Su 10:00-18:00; Tu[-1] off"
    assert hours.day_intervals(museum, date(2026, 10, 20)) == [
        (datetime(2026, 10, 20, 10, tzinfo=tz), datetime(2026, 10, 20, 18, tzinfo=tz))]
    assert hours.day_intervals(museum, date(2026, 10, 27)) == []  # last Tuesday: sanitary day
    bar = hours.day_intervals("Mo-Su 18:00-04:00", date(2026, 10, 23))
    assert bar == [(datetime(2026, 10, 23, 18, tzinfo=tz), datetime(2026, 10, 24, 4, tzinfo=tz))]


def test_kazakhstan_public_holidays():
    rule = "Mo-Su 10:00-18:00; PH off"
    assert hours.day_intervals(rule, date(2026, 10, 25)) == []  # Republic Day
    assert hours.day_intervals(rule, date(2026, 10, 24)) != []
    assert not hours.is_valid("not a valid rule")


# ---------- JSON-LD ----------
def test_jsonld_exhibition():
    evs = jsonld.extract(fixture_text("event_jsonld.html"), "https://sxodim.com/astana/event/vystavka-svet-velikoy-stepi")
    assert len(evs) == 1
    e = evs[0]
    assert e.kind == "exhibition"
    assert (e.date_start, e.date_end) == ("2026-10-01", "2026-10-26")
    assert e.venue_name == "Национальный музей РК"
    assert e.address == "пр. Тауельсиздик, 54, Астана"
    assert e.price_min == 1500 and e.is_free is False
    assert e.description == "Живопись и графика казахстанских художников о степи."
    assert e.image_url == "https://example.org/step.jpg"


def test_jsonld_cancelled_concert_times_in_astana_tz():
    e = jsonld.extract(fixture_text("event_cancelled_jsonld.html"), "https://x")[0]
    assert e.status == "cancelled"
    assert (e.date_start, e.time_start, e.time_end) == ("2026-10-23", "19:00", "21:00")
    assert e.price_min == 3000


# ---------- page text & links ----------
def test_main_text_drops_related_events_but_keeps_title_and_date():
    pt = main_text(fixture_text("event_page_text.html"))
    assert pt.title == "Вечерняя выставка «Ночь в музее»"
    assert "Когда: 23 октября, с 18:00 до 22:00" in pt.text
    assert "Концерт «Осень»" not in pt.text and "Бронза" not in pt.text
    assert "Поделиться" not in pt.text and "Пользовательское соглашение" not in pt.text
    assert pt.og_image == "https://example.org/noch.jpg"


def test_evidence_check():
    src = "Когда: 23 октября,   с 18:00 до 22:00\nГде: музей"
    assert evidence_found("Когда: 23 октября, с 18:00 до 22:00", src)
    assert not evidence_found("Когда: 24 октября, с 19:00 до 23:00", src)
    assert not evidence_found(None, src)


def test_find_links_listing():
    links = find_links(fixture_text("listing.html"), "https://sxodim.com/astana/afisha",
                       r"^https://sxodim\.com/astana/event/[a-z0-9-]+/?$")
    assert links == [
        "https://sxodim.com/astana/event/vystavka-svet-velikoy-stepi",
        "https://sxodim.com/astana/event/noch-v-muzee",
        "https://sxodim.com/astana/event/koncert-osennie-melodii",
    ]


# ---------- Telegram ----------
def test_telegram_posts_and_filter():
    posts = telegram.parse_channel_page(fixture_text("telegram_channel.html"))
    assert [p.post_id for p in posts] == ["90", "101", "102"]
    assert posts[1].url == "https://t.me/test_afisha_channel/101"
    assert "джаз-вечер" in posts[1].text
    fresh = telegram.fresh_event_posts(posts, max_age_days=10, now=datetime(2026, 10, 23, 12, tzinfo=timezone.utc))
    assert [p.post_id for p in fresh] == ["101"]  # 90 is too old, 102 is not an event
