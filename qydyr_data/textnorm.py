"""Text normalization for matching names across Russian, Kazakh and Latin spellings.

"Хан Шатыр", "Хан-Шатыр", "Khan Shatyr" and "ТРЦ «Хан Шатыр»" must all land on the same key.
"""
from __future__ import annotations

import re
import unicodedata

_CYR2LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
    "и": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sh",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    # Kazakh letters
    "ә": "a", "ғ": "g", "қ": "k", "ң": "n", "ө": "o", "ұ": "u", "ү": "u", "һ": "h", "і": "i",
}

# Words that describe the type of a venue rather than its name.
GENERIC_WORDS = {
    "трц", "тц", "тоо", "ип", "г", "город", "астана", "astana", "nur-sultan",
    "кафе", "cafe", "coffee", "кофейня", "ресторан", "restaurant", "бар", "bar",
    "зал", "hall", "центр", "center", "centre",
}

_QUOTES = "«»„“”\"'`’‘"
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES_RE = re.compile(r"\s+")


def basic(text: str | None) -> str:
    """Lowercase, unify quotes/dashes/ё, drop punctuation, collapse spaces."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text).lower().replace("ё", "е")
    for q in _QUOTES:
        t = t.replace(q, " ")
    t = re.sub(r"[‐-―−-]", " ", t)  # all dashes/hyphens to spaces
    t = _PUNCT_RE.sub(" ", t)
    return _SPACES_RE.sub(" ", t).strip()


def translit(text: str) -> str:
    """Cyrillic (ru + kk) to a simple Latin form."""
    return "".join(_CYR2LAT.get(ch, ch) for ch in text)


def phonetic(latin: str) -> str:
    """Coarse Latin key that forgives common romanization differences."""
    t = latin
    for a, b in (("kh", "h"), ("y", "i"), ("q", "k"), ("w", "v"), ("ts", "c"), ("iy", "i"), ("ii", "i")):
        t = t.replace(a, b)
    return t


def strip_generic(text: str) -> str:
    words = [w for w in text.split() if w not in GENERIC_WORDS]
    return " ".join(words) if words else text


def name_keys(name: str | None) -> set[str]:
    """All comparable keys for a name: plain, without generic words, transliterated, phonetic."""
    b = basic(name)
    if not b:
        return set()
    keys = {b, strip_generic(b)}
    for k in list(keys):
        lat = translit(k)
        keys.add(lat)
        keys.add(phonetic(lat))
    return {k for k in keys if k}


def match_key(name: str | None) -> str:
    """Single canonical key (used for event ids and exact lookups)."""
    return phonetic(translit(strip_generic(basic(name))))


def squash(text: str | None) -> str:
    """Whitespace/quote/dash-insensitive form for verbatim evidence checks."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text).lower().replace("ё", "е")
    for q in _QUOTES:
        t = t.replace(q, '"')
    t = re.sub(r"[‐-―−]", "-", t)
    return _SPACES_RE.sub(" ", t).strip()
