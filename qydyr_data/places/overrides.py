"""Apply config/place_overrides.yaml (aliases, indoor flag, extra attrs) to normalized places."""
from __future__ import annotations

from pathlib import Path

import yaml

from .. import settings
from ..textnorm import name_keys
from .normalize import Place


def load(path: Path | None = None) -> dict:
    p = path or settings.CONFIG_DIR / "place_overrides.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}


def apply(places: list[Place], overrides: dict) -> int:
    """Returns how many places were touched."""
    rules = []
    for name, rule in (overrides or {}).items():
        rule = rule or {}
        keys = name_keys(name)
        for a in rule.get("aliases", []):
            keys |= name_keys(a)
        rules.append((name, rule, keys))

    touched = 0
    for p in places:
        pkeys: set[str] = set()
        for n in (p.name, p.name_ru, p.name_kk, p.name_en, *p.aliases):
            pkeys |= name_keys(n)
        for name, rule, keys in rules:
            if pkeys & keys:
                p.aliases = sorted({*p.aliases, name, *rule.get("aliases", [])} - {p.name})
                if rule.get("indoor"):
                    p.indoor = rule["indoor"]
                p.attrs.update(rule.get("attrs", {}))
                touched += 1
                break
    return touched
