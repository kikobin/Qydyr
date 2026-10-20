"""Answer (the API's JSON) -> Telegram HTML and keyboards. Pure functions: no network, easy to test.
Keyboards are lists of rows of plain dicts; `markup()` turns them into aiogram objects.
Labels come from texts.py; the data itself (statuses, reasons, facts) is shown as the API gave it."""
from __future__ import annotations

from datetime import datetime, timedelta
from html import escape
from urllib.parse import urlsplit

from .. import settings
from .texts import T, plural, t

WEEKDAYS = {"ru": ["пн", "вт", "ср", "чт", "пт", "сб", "вс"], "kk": ["дс", "сс", "ср", "бс", "жм", "сб", "жс"],
            "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]}


def dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s).astimezone(settings.TZ) if s else None


def hm(s: str | None) -> str:
    d = dt(s)
    return d.strftime("%H:%M") if d else ""


def host(u: str) -> str:
    h = urlsplit(u).netloc.replace("www.", "")
    return "OpenStreetMap" if "openstreetmap" in h else h or u


def in_astana(lat: float, lon: float) -> bool:
    s, w, n, e = settings.ASTANA_BBOX
    return s <= lat <= n and w <= lon <= e


def conf(c: float, lang: str = "ru") -> str:
    return t(lang, "conf_high" if c >= 0.75 else "conf_medium" if c >= 0.5 else "conf_low")


def walk_text(it: dict, lang: str = "ru") -> str:
    if it["walk_min"] == 0:
        return t(lang, "right_here")
    return t(lang, "by_taxi" if it.get("by") == "taxi" else "by_walk", n=it["walk_min"])


def minutes(m: int, lang: str = "ru") -> str:
    """A time budget: 30 мин, 1,5 ч / 1.5 h."""
    if m < 60:
        return t(lang, "min", n=m)
    h = f"{m / 60:g}"
    return f"{h if lang == 'en' else h.replace('.', ',')} {t(lang, 'h')}"


def chip_label(c: dict, ans: dict, lang: str = "ru") -> str:
    """Interface chips in the interface language; needs (кофе, тихо) stay as the API gave them."""
    if lang == "ru":
        return c["label"]
    it, k = ans.get("intent") or {}, c["key"]
    if k == "kids":
        return t(lang, "chip_kids")
    if k == "only_events":
        return t(lang, "chip_events")
    if k == "wheelchair":
        return t(lang, "chip_wheelchair")
    if k == "group" and it.get("group_size"):
        return t(lang, "chip_group", n=it["group_size"])
    if k == "mode" and it.get("mode") in ("walk", "taxi", "any"):
        return t(lang, f"mode_{it['mode']}")
    if k == "budget":
        x = c["label"]
        up = x.startswith("до ")
        x = (x[3:] if up else x).replace(" мин", " min" if lang == "en" else " мин").replace(" ч", " " + t(lang, "h"))
        return t(lang, "upto", x=x) if up else x
    return c["label"]


def option_label(c: dict, o: dict, lang: str = "ru") -> str:
    p = o.get("patch") or {}
    if lang != "ru" and c["key"] == "budget" and p.get("budget_max"):
        return minutes(p["budget_max"], lang)
    if lang != "ru" and c["key"] == "mode" and p.get("mode") in ("walk", "taxi"):
        return t(lang, f"mode_{p['mode']}")
    return o["label"]


def understood(ans: dict, lang: str = "ru") -> str:
    return " · ".join(chip_label(c, ans, lang) + ("?" if c["assumed"] else "") for c in ans["chips"]
                      if c["active"] and c["kind"] != "place")


def funnel_text(x: dict, lang: str = "ru") -> str:
    key = f"funnel_{x['key']}"
    return plural(lang, x["n"], t(lang, key)) if key in T["ru"] else x["label"]


def anchor_label(ans: dict, lang: str = "ru", gps_age: int | None = None) -> str:
    a = ans.get("anchor") or {}
    if a.get("source") == "gps":
        return t(lang, "gps_age", n=gps_age) if gps_age else t(lang, "gps_label")
    return a.get("label", "")


def answer_html(ans: dict, lang: str = "ru", *, compact: bool = False, gps_age: int | None = None) -> str:
    """gps_age: minutes since the session's location was shared, shown in the header ("15 мин назад")."""
    e = escape
    at = dt(ans["at"])
    head = [f"{WEEKDAYS.get(lang, WEEKDAYS['ru'])[at.weekday()]} {at:%H:%M}"]
    ctx = ans.get("context") or {}
    w = ctx.get("weather")
    if w:
        head.append(f"{w['temp_c']:+.0f}°, {w['level']}")
        if w.get("sunset"):
            head.append(t(lang, "sunset", time=hm(w["sunset"])))
    lines = [f"<b>{e(' · '.join(head))}</b>"]
    if ans.get("anchor"):
        lines[0] = f"<b>{e(anchor_label(ans, lang, gps_age))}</b> · " + lines[0]
    if ctx.get("simulated"):
        lines.append(f"<i>{e(t(lang, 'simulated', what=ctx['simulated']))}</i>")
    u = understood(ans, lang)
    if u:
        lines.append(e(t(lang, "understood", u=u)))
    notes = [n for n in ans.get("notes", []) if not n.startswith("Симуляция")]
    for n in notes[:3]:
        lines.append(f"<i>{e(n)}</i>")
    for i, it in enumerate(ans["items"], 1):
        lines.append("")
        lines.append(f"<b>{i}. {e(it['title'])}</b> — {e(walk_text(it, lang))}")
        lines.append(e(it["status"]))
        if it.get("leave_by"):
            lb = dt(it["leave_by"])
            lines.append(f"<b>{e(t(lang, 'leave_now'))}</b>" if lb <= dt(ans["at"]) + timedelta(minutes=1)
                         else f"<b>{e(t(lang, 'leave_by', time=f'{lb:%H:%M}'))}</b>")
        details = []
        if it.get("why"):
            details.append(f"{t(lang, 'why')}: {e('; '.join(it['why']))}")
        if it.get("group_fit"):
            details.append(f"{t(lang, 'everyone')}: " + e(" · ".join(it["group_fit"])))
        lo, hi = it["visit_min"]
        details.append(f"{t(lang, 'how_long')}: {e(t(lang, 'min', n=lo if lo == hi else f'{lo}–{hi}'))}")
        src = t(lang, f"src_{it['what_to_do_source']}")
        details.append(f"{e(t(lang, 'what_to_do', src=src))}: {e(it['what_to_do'])}")
        if it.get("warnings"):
            details.append(f"{t(lang, 'warning')}: {e('; '.join(it['warnings']))}")
        hosts = ", ".join(dict.fromkeys(host(s) for s in it.get("sources", []))) or "OpenStreetMap"
        details.append(e(t(lang, "source", src=hosts, conf=conf(it["confidence"], lang))))
        if compact and i > 1:
            continue
        lines.append("<blockquote expandable>" + "\n".join(details) + "</blockquote>")
    if not ans["items"] and not notes:   # the "nothing fits" note is already printed above
        lines.append("")
        lines.append(e(t(lang, "nothing_fits")))
    r = ans.get("route")
    if r and not compact:
        lines.append("")
        lines.append(f"<b>{e(t(lang, 'mini_route', total=r['total_min']))}</b> ({e(t(lang, 'outdoor', n=r['outdoor_min']))})")
        for s in r["stops"]:
            lines.append(f"{hm(s['arrive_at'])}–{hm(s['leave_at'])}  {e(s['title'])}")
    for g in ans.get("group_notes", []):
        lines.append(f"<i>{e(g)}</i>")
    f = ans.get("funnel") or []
    if f:
        lines.append("")
        lines.append(f"<i>{e(t(lang, 'how_chosen'))}: " + e(" → ".join(f"{x['n']} {funnel_text(x, lang)}" for x in f)) + "</i>")
    if ans.get("anchor") and ans["anchor"]["source"] == "gps":
        lines.append(f"<i>{e(t(lang, 'privacy'))}</i>")
    return "\n".join(lines)


def item_html(ans: dict, i: int, lang: str = "ru") -> str:
    """One recommendation as a self-contained message (inline mode, group result)."""
    e = escape
    it = ans["items"][i]
    lines = [f"<b>{e(it['title'])}</b> — {e(walk_text(it, lang))}", e(it["status"])]
    if it.get("why"):
        lines.append(f"{t(lang, 'why')}: {e('; '.join(it['why'][:2]))}")
    src = t(lang, f"src_{it['what_to_do_source']}")
    lines.append(f"{e(t(lang, 'what_to_do', src=src))}: {e(it['what_to_do'])}")
    where = ans["anchor"]["label"] if ans.get("anchor") and ans["anchor"]["source"] != "gps" else ""
    lines.append(f"<i>{e(t(lang, 'item_from', place=where, time=hm(ans['at'])))}</i>" if where
                 else f"<i>Qydyr · {hm(ans['at'])}</i>")
    return "\n".join(lines)


def why_not_html(ans: dict, lang: str = "ru") -> str:
    rows = [f"<b>{escape(t(lang, 'why_not').rstrip('?'))}</b>"]
    for x in ans.get("funnel") or []:
        rows.append(f"{x['n']:>4}  {escape(funnel_text(x, lang))}")
    if ans.get("rejected"):
        rows.append("")
        rows += [f"<b>{escape(r['title'])}</b> — {escape(r['reason'])}" for r in ans["rejected"][:7]]
    rows.append("")
    rows.append(f"<i>{escape(t(lang, 'why_not_footer'))}</i>")
    return "\n".join(rows)


def _cb(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def answer_keyboard(ans: dict, lang: str = "ru", *, private: bool = True, webapp_url: str | None = None,
                    bot_username: str | None = None) -> list[list[dict]]:
    """In a group the chips are left out: they would change the shared answer but not the poll under it.
    The map button needs the Mini App: no HTTPS address, no button."""
    aid = ans["id"]
    rows: list[list[dict]] = []
    items = ans["items"]
    if items:
        rows.append([_cb(t(lang, "route", n=i + 1) if len(items) > 1 else t(lang, "route_one"), f"m:{aid}:{i}")
                     for i in range(len(items))])
    row = []
    if webapp_url and private:
        row.append({"text": t(lang, "map"), "web_app": {"url": f"{webapp_url}?answer={aid}"}})
    elif webapp_url and bot_username:
        row.append({"text": t(lang, "map"), "url": f"https://t.me/{bot_username}?startapp=a_{aid}"})
    if ans.get("rejected") or ans.get("funnel"):
        row.append(_cb(t(lang, "why_not"), f"w:{aid}"))
    if row:
        rows.append(row)
    chips = []
    for ci, c in enumerate(ans["chips"] if private else []):
        if c["key"] == "budget" or (c["key"] == "mode" and c["assumed"]):
            chips.append(_cb(f"{chip_label(c, ans, lang)} · {t(lang, 'change')}", f"c:{aid}:{ci}"))
        elif c["key"] in ("kids", "only_events"):   # a switch: "+ с ребёнком" on, "✕ с ребёнком" off
            chips.append(_cb(t(lang, "chip_remove" if c["active"] else "chip_add", label=chip_label(c, ans, lang)),
                             f"c:{aid}:{ci}"))
    for k in range(0, len(chips), 2):
        rows.append(chips[k:k + 2])
    if items:
        rows.append([_cb(t(lang, "good"), f"f:{aid}:up"), _cb(t(lang, "bad"), f"f:{aid}:down")])
    return rows


def chip_keyboard(ans: dict, ci: int, lang: str = "ru") -> list[list[dict]]:
    aid, chip = ans["id"], ans["chips"][ci]
    if chip["key"] in ("kids", "only_events", "wheelchair") or len(chip["options"]) == 1:
        return []  # a switch: applied at once
    opts = [_cb(("· " if o["selected"] else "") + option_label(chip, o, lang), f"c:{aid}:{ci}:{oi}")
            for oi, o in enumerate(chip["options"])]
    rows = [opts[k:k + 3] for k in range(0, len(opts), 3)]
    rows.append([_cb(t(lang, "back"), f"k:{aid}")])
    return rows


def route_keyboard(lat: float, lon: float, lang: str = "ru") -> list[list[dict]]:
    names = t(lang, "maps").split("|")
    urls = [f"https://2gis.kz/astana/directions/points/%7C{lon}%2C{lat}",
            f"https://yandex.kz/maps/?rtext=~{lat},{lon}&rtt=pd",
            f"https://www.google.com/maps/dir/?api=1&destination={lat},{lon}&travelmode=walking"]
    return [[{"text": t(lang, "open_in", app=name), "url": url}] for name, url in zip(names, urls)]


def poll_options(items: list[dict], lang: str = "ru") -> list[str]:
    """Telegram rejects a poll with two equal options: "Детская площадка · 7 мин пешком", numbered if still equal."""
    opts = [it["title"][:100] for it in items]
    opts = [f"{o} · {walk_text(it, lang)}"[:100] if opts.count(o) > 1 else o for o, it in zip(opts, items)]
    if len(set(opts)) < len(opts):
        opts = [f"{i}. {o}"[:100] for i, o in enumerate(opts, 1)]
    return opts


def walk_route_url(stops: list[dict]) -> str | None:
    """All stops of a walk as one Google Maps walking route (from wherever the person is now)."""
    pts = [f"{s['lat']},{s['lon']}" for s in stops if s.get("lat") is not None][:10]
    if not pts:
        return None
    url = f"https://www.google.com/maps/dir/?api=1&destination={pts[-1]}&travelmode=walking"
    return url + (f"&waypoints={'%7C'.join(pts[:-1])}" if len(pts) > 1 else "")


def markup(rows: list[list[dict]]):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
    out = []
    for row in rows:
        btns = []
        for b in row:
            if "web_app" in b:
                btns.append(InlineKeyboardButton(text=b["text"], web_app=WebAppInfo(url=b["web_app"]["url"])))
            elif "url" in b:
                btns.append(InlineKeyboardButton(text=b["text"], url=b["url"]))
            else:
                btns.append(InlineKeyboardButton(text=b["text"], callback_data=b["callback_data"]))
        out.append(btns)
    return InlineKeyboardMarkup(inline_keyboard=out)


def meters(m: int | None, lang: str = "ru") -> str:
    if m is None:
        return ""
    if m >= 1000:
        km = f"{m / 1000:.1f}"
        return f"{km} km" if lang == "en" else f"{km.replace('.', ',')} км"
    return f"{m} m" if lang == "en" else f"{m} м"


def tour_html(tour: dict) -> str:
    """The tour card: title, meta, the mascot's intro and the numbered stops."""
    stops = "\n".join(f"{s['n']}. {escape(s['name'])}" for s in tour["stops_list"])
    hours = f"\n\n<i>{escape(tour['hours']['text'])}</i>" if tour.get("hours") else ""
    return f"<b>{escape(tour['title'])}</b>\n{escape(tour['meta'])}\n\n{escape(tour['intro'])}\n\n{stops}{hours}"


def stop_html(tour: dict, stop: dict, lang: str = "ru", nxt: dict | None = None, next_m: int | None = None,
              next_min: int | None = None) -> str:
    """One stop of a guided walk: its facts with source links, what to do, and where next."""
    lines = [f"<b>{stop['n']}/{len(tour['stops_list'])} · {escape(stop['name'])}</b>"]
    for f in stop["facts"]:
        src = f' <a href="{escape(f["source"], quote=True)}">{escape(f["source_name"])}</a>' if f.get("source") else ""
        lines.append(f"• {escape(f['text'])}{src}")
    if stop.get("tip"):
        lines.append(t(lang, "stop_tip", tip=escape(stop["tip"])))
    if nxt:
        lines.append("")
        lines.append(t(lang, "stop_next", name=escape(nxt["name"]), dist=meters(next_m, lang), min=next_min)
                     if next_m is not None else t(lang, "stop_next_plain", name=escape(nxt["name"])))
    return "\n".join(lines)
