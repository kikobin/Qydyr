"""Answer (the API's JSON) -> Telegram HTML and keyboards. Pure functions: no network, easy to test.
Keyboards are lists of rows of plain dicts; `markup()` turns them into aiogram objects."""
from __future__ import annotations

from datetime import datetime, timedelta
from html import escape
from urllib.parse import urlsplit

from .. import settings
from .texts import t

WEEKDAYS = {"ru": ["пн", "вт", "ср", "чт", "пт", "сб", "вс"], "kk": ["дс", "сс", "ср", "бс", "жм", "сб", "жс"],
            "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]}
SRC = {"data": "из афиши", "idea": "идея ассистента"}
CONF = lambda c: "высокая" if c >= 0.75 else "средняя" if c >= 0.5 else "низкая"  # noqa: E731


def dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s).astimezone(settings.TZ) if s else None


def hm(s: str | None) -> str:
    d = dt(s)
    return d.strftime("%H:%M") if d else ""


def host(u: str) -> str:
    h = urlsplit(u).netloc.replace("www.", "")
    return "OpenStreetMap" if "openstreetmap" in h else h or u


def walk_text(it: dict) -> str:
    if it["walk_min"] == 0:
        return "прямо здесь"
    return f"{it['walk_min']} мин {'на такси' if it.get('by') == 'taxi' else 'пешком'}"


def understood(ans: dict) -> str:
    return " · ".join(c["label"] + ("?" if c["assumed"] else "") for c in ans["chips"] if c["active"]
                      and c["kind"] != "place")


def answer_html(ans: dict, lang: str = "ru", *, compact: bool = False) -> str:
    e = escape
    at = dt(ans["at"])
    head = [f"{WEEKDAYS.get(lang, WEEKDAYS['ru'])[at.weekday()]} {at:%H:%M}"]
    ctx = ans.get("context") or {}
    w = ctx.get("weather")
    if w:
        head.append(f"{w['temp_c']:+.0f}°, {w['level']}")
        if w.get("sunset"):
            head.append(f"закат {hm(w['sunset'])}")
    lines = [f"<b>{e(' · '.join(head))}</b>"]
    if ans.get("anchor") and ans["anchor"]["source"] != "gps":
        lines[0] = f"<b>{e(ans['anchor']['label'])}</b> · " + lines[0]
    if ctx.get("simulated"):
        lines.append(f"<i>Симуляция: {e(ctx['simulated'])}. Места, часы работы и события настоящие.</i>")
    u = understood(ans)
    if u:
        lines.append(f"Понял так: {e(u)}")
    notes = [n for n in ans.get("notes", []) if not n.startswith("Симуляция")]
    for n in notes[:3]:
        lines.append(f"<i>{e(n)}</i>")
    for i, it in enumerate(ans["items"], 1):
        lines.append("")
        lines.append(f"<b>{i}. {e(it['title'])}</b> — {e(walk_text(it))}")
        lines.append(e(it["status"]))
        if it.get("leave_by"):
            lb = dt(it["leave_by"])
            lines.append("<b>Выходите сейчас</b>" if lb <= dt(ans["at"]) + timedelta(minutes=1)
                         else f"<b>Выходите до {lb:%H:%M}</b>")
        details = []
        if it.get("why"):
            details.append(f"Почему: {e('; '.join(it['why']))}")
        if it.get("group_fit"):
            details.append("Каждому: " + e(" · ".join(it["group_fit"])))
        lo, hi = it["visit_min"]
        details.append(f"Сколько: {lo if lo == hi else f'{lo}–{hi}'} мин")
        details.append(f"Что делать ({SRC[it['what_to_do_source']]}): {e(it['what_to_do'])}")
        if it.get("warnings"):
            details.append(f"Внимание: {e('; '.join(it['warnings']))}")
        src = ", ".join(dict.fromkeys(host(s) for s in it.get("sources", []))) or "OpenStreetMap"
        details.append(f"Источник: {e(src)} · уверенность {CONF(it['confidence'])}")
        if compact and i > 1:
            continue
        lines.append("<blockquote expandable>" + "\n".join(details) + "</blockquote>")
    if not ans["items"]:
        lines.append("")
        lines.append(e(ans["notes"][-1]) if ans.get("notes") else "Рядом ничего не подошло.")
    r = ans.get("route")
    if r and not compact:
        lines.append("")
        lines.append(f"<b>Мини-маршрут на {r['total_min']} мин</b> (на улице ~{r['outdoor_min']} мин)")
        for s in r["stops"]:
            lines.append(f"{hm(s['arrive_at'])}–{hm(s['leave_at'])}  {e(s['title'])}")
    for g in ans.get("group_notes", []):
        lines.append(f"<i>{e(g)}</i>")
    f = ans.get("funnel") or []
    if f:
        lines.append("")
        lines.append("<i>Как выбирал: " + e(" → ".join(f"{x['n']} {x['label']}" for x in f)) + "</i>")
    if ans.get("anchor") and ans["anchor"]["source"] == "gps":
        lines.append(f"<i>{e(t(lang, 'privacy'))}</i>")
    return "\n".join(lines)


def item_html(ans: dict, i: int) -> str:
    """One recommendation as a self-contained message (inline mode, group result)."""
    e = escape
    it = ans["items"][i]
    lines = [f"<b>{e(it['title'])}</b> — {e(walk_text(it))}", e(it["status"])]
    if it.get("why"):
        lines.append(f"Почему: {e('; '.join(it['why'][:2]))}")
    lines.append(f"Что делать ({SRC[it['what_to_do_source']]}): {e(it['what_to_do'])}")
    where = ans["anchor"]["label"] if ans.get("anchor") else ""
    lines.append(f"<i>Qydyr · от «{e(where)}» · {hm(ans['at'])}</i>" if where else f"<i>Qydyr · {hm(ans['at'])}</i>")
    return "\n".join(lines)


def why_not_html(ans: dict, lang: str = "ru") -> str:
    rows = [f"<b>{escape(t(lang, 'why_not').rstrip('?'))}</b>"]
    for x in ans.get("funnel") or []:
        rows.append(f"{x['n']:>4}  {escape(x['label'])}")
    if ans.get("rejected"):
        rows.append("")
        rows += [f"<b>{escape(r['title'])}</b> — {escape(r['reason'])}" for r in ans["rejected"][:7]]
    rows.append("")
    rows.append("<i>Выбирает алгоритм по фактам: часы работы, расстояние, погода, события. "
                "Claude только понимает запрос и читает афиши.</i>")
    return "\n".join(rows)


def _cb(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def answer_keyboard(ans: dict, lang: str = "ru", *, private: bool = True, webapp_url: str | None = None,
                    bot_username: str | None = None) -> list[list[dict]]:
    aid = ans["id"]
    rows: list[list[dict]] = []
    items = ans["items"]
    if items:
        rows.append([_cb(t(lang, "route", n=i + 1) if len(items) > 1 else "Маршрут", f"m:{aid}:{i}")
                     for i in range(len(items))])
    row = []
    if webapp_url and private:
        row.append({"text": t(lang, "map"), "web_app": {"url": f"{webapp_url}?answer={aid}"}})
    elif bot_username:
        row.append({"text": t(lang, "map"), "url": f"https://t.me/{bot_username}?startapp=a_{aid}"})
    if ans.get("rejected") or ans.get("funnel"):
        row.append(_cb(t(lang, "why_not"), f"w:{aid}"))
    if row:
        rows.append(row)
    chips = []
    for ci, c in enumerate(ans["chips"]):
        if c["key"] == "budget" or (c["key"] in ("kids", "only_events") and not c["active"]) \
                or (c["key"] == "mode" and c["assumed"]):
            label = f"{c['label']} · {t(lang, 'change')}" if c["key"] in ("budget", "mode") else f"+ {c['label']}"
            chips.append(_cb(label, f"c:{aid}:{ci}"))
    for k in range(0, len(chips), 2):
        rows.append(chips[k:k + 2])
    if items:
        rows.append([_cb(t(lang, "good"), f"f:{aid}:up"), _cb(t(lang, "bad"), f"f:{aid}:down")])
    return rows


def chip_keyboard(ans: dict, ci: int, lang: str = "ru") -> list[list[dict]]:
    aid, chip = ans["id"], ans["chips"][ci]
    if chip["key"] in ("kids", "only_events", "wheelchair") or len(chip["options"]) == 1:
        return []  # a switch: applied at once
    opts = [_cb(("· " if o["selected"] else "") + o["label"], f"c:{aid}:{ci}:{oi}") for oi, o in enumerate(chip["options"])]
    rows = [opts[k:k + 3] for k in range(0, len(opts), 3)]
    rows.append([_cb(t(lang, "back"), f"k:{aid}")])
    return rows


def route_keyboard(lat: float, lon: float, lang: str = "ru") -> list[list[dict]]:
    links = {"2ГИС": f"https://2gis.kz/astana/directions/points/%7C{lon}%2C{lat}",
             "Яндекс Карты": f"https://yandex.kz/maps/?rtext=~{lat},{lon}&rtt=pd",
             "Google Maps": f"https://www.google.com/maps/dir/?api=1&destination={lat},{lon}&travelmode=walking"}
    return [[{"text": t(lang, "open_in", app=name), "url": url}] for name, url in links.items()]


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
