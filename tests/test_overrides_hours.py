"""Hours OpenStreetMap lacks come from config/place_overrides.yaml, only with a source, never onto a namesake LRT stop."""
from qydyr_data.places import overrides
from qydyr_data.places.normalize import Place

RULES = {"Национальный музей": {"aliases": ["Ұлттық Мұражай"], "opening_hours": "Tu-Su 10:00-18:00; Mo off",
                                "hours_source": "https://nmrk.kz/about-museum/contacts/"},
         "Без источника": {"opening_hours": "Mo-Su 10:00-20:00"}}


def place(pid, name, category="culture", subcategory="museum", oh=None):
    return Place(id=pid, name=name, category=category, subcategory=subcategory, lat=51.12, lon=71.47,
                 opening_hours=oh, hours_valid=bool(oh))


def test_sourced_hours_fill_the_gap_and_osm_wins():
    museum, stop = place("m", "Ұлттық Мұражай"), place("s", "Ұлттық Мұражай", "anchor", "railway_station")
    known, unsourced = place("k", "Национальный музей", oh="Mo-Su 09:00-19:00"), place("u", "Без источника")
    overrides.apply([museum, stop, known, unsourced], RULES)
    assert museum.hours_valid and museum.attrs["opening_hours:source"].startswith("https://nmrk.kz")
    assert not stop.hours_valid and "opening_hours:source" not in stop.attrs and "Национальный музей" in stop.aliases
    assert known.opening_hours == "Mo-Su 09:00-19:00"          # OpenStreetMap's own hours are kept
    assert not unsourced.hours_valid                             # no source, no hours
