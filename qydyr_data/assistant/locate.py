"""Where is the person? GPS if shared, otherwise the place they named, in any spelling or case form:
"возле Mega Silk Way", "у Чупа-чупса", "в AITU", "у здания Экспо 2017"."""
from __future__ import annotations

import re

from ..events.venues import PlaceLite, VenueMatcher
from ..textnorm import name_keys
from .models import Anchor, Intent

SELF_AT = re.compile(r"\b(я|мы|i'?m|i am|we'?re|we are|мен|біз)\b[^.!?]{0,24}?\b(у|возле|около|рядом с|в районе|"
                     r"напротив|near|at|by|жанында|қасында|маңында)\b", re.I)


class Locator:
    def __init__(self, places: list[PlaceLite]) -> None:
        self.places = {p.id: p for p in places}
        self.matcher = VenueMatcher(places)

    def _exact_subphrase(self, text: str) -> PlaceLite | None:
        words = re.findall(r"[\w-]+", text)
        for n in range(min(5, len(words)), 0, -1):  # longest phrase first
            for i in range(len(words) - n + 1):
                phrase = " ".join(words[i:i + n])
                ids: set[str] = set()
                for k in name_keys(phrase):
                    if len(k) >= 3:
                        ids |= self.matcher.key_to_ids.get(k, set())
                if len(ids) == 1:
                    return self.places[next(iter(ids))]
        return None

    def find_place(self, text: str | None) -> PlaceLite | None:
        if not text:
            return None
        if p := self._exact_subphrase(text):
            return p
        m = self.matcher.match(text)
        if m:
            return m.place
        # Russian case endings: "Байтерека", "Хан Шатыра" -> drop 1–2 trailing letters of the last word
        words = text.split()
        for cut in (1, 2):
            if len(words[-1]) > 4 + cut:
                m = self.matcher.match(" ".join(words[:-1] + [words[-1][:-cut]]))
                if m:
                    return m.place
        return None

    def resolve(self, intent: Intent) -> Anchor | None:
        if intent.lat is not None and intent.lon is not None:
            # "я у Хан Шатыра" beats GPS: the person told us where they are (or will be)
            if SELF_AT.search(intent.text) and (place := self.find_place(intent.anchor_text)):
                return Anchor(lat=place.lat, lon=place.lon, label=place.name, source="place", place_id=place.id)
            return Anchor(lat=intent.lat, lon=intent.lon, label="ваша геолокация", source="gps")
        place = self.find_place(intent.anchor_text) or self.find_place(intent.text)
        if place:
            return Anchor(lat=place.lat, lon=place.lon, label=place.name, source="place", place_id=place.id)
        return None

    def airport(self) -> PlaceLite | None:
        return next((p for p in self.places.values() if p.subcategory == "airport"), None)
