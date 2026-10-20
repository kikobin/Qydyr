// Qydyr Mini App: map + sheet. Works inside Telegram and in any browser.
import { api } from "./api.js";
import { WeatherFx } from "./fx.js";
import { QUICK_QUERIES, SPEECH_LANG, STR, UI, WALK } from "./i18n.js";
import { ASYQ, I, REASON_ICON } from "./icons.js";
import { ASTANA, CityMap } from "./map.js";
import { MASCOT_NAME, mascot, moodForWeather } from "./mascot.js";
import * as T from "./tg.js";
import * as WK from "./walk.js";

const $ = (s, r = document) => r.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const TZ_OFFSET_MIN = 5 * 60;  // Astana: UTC+5 all year
const local = (iso) => new Date(new Date(iso).getTime() + TZ_OFFSET_MIN * 60000);
const hm = (iso) => { if (!iso) return ""; const d = local(iso); return `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`; };
const isoLocal = (date) => { const d = new Date(date.getTime() + TZ_OFFSET_MIN * 60000); return d.toISOString().slice(0, 19) + "+05:00"; };

const S = {
  lang: "ru", t: STR.ru, theme: "light", config: null,
  loc: null, query: "", overrides: {}, preset: null, at: null,
  answer: null, sel: 0, view: "home", stack: [], place: null, walks: null, walkTitle: null,
  loading: false, favs: [], sheet: "half",
  tours: null, tour: null, walk: null, walkDone: null, saved: null, wx: null, walkTour: null,
};
const W = () => WALK[S.lang];
const DEMO_MPS = 95;      // the demo walker's speed on screen, meters per second

let map = null;
let fx = null;

// ---------------------------------------------------------------- helpers
function toast(text, ms = 2600) {
  const t = document.createElement("div");
  t.className = "toast";
  t.setAttribute("role", "status");
  t.textContent = text;
  $("#app").append(t);
  setTimeout(() => t.remove(), ms);
}

function setSheet(state) {
  S.sheet = state;
  const peek = `calc(var(--app-h) * ${{ low: 0.24, half: 0.48, full: 0.88 }[state]})`;   // of what Telegram shows
  $("#sheet").style.setProperty("--peek", peek);
}

// how far each screen opens the sheet: the pages (home, walks, saved) cover the map, results leave it in view
const SHEET_FOR = { home: "full", tours: "full", saved: "full", place: "full", whynot: "full", results: "half", machine: "half" };

function go(view, push = true) {
  const at = S.stack.lastIndexOf(view);
  if (push && at >= 0) S.stack.length = at;          // results ⇄ machine ⇄ results is a step back, not three forward
  else if (push && S.view !== view) S.stack.push(S.view);
  if (SHEET_FOR[view] && S.view !== view) setSheet(SHEET_FOR[view]);
  S.view = view;
  T.backButton(S.stack.length > 0);
  renderSheet();
  $("#sheet .body").scrollTop = 0;
}

function back() {
  const v = S.stack.pop();
  if (v == null) return;
  if (S.view === "walk") stopWalk();
  if ((S.view === "tour" || S.view === "walk" || S.view === "walkdone") && v !== "tour" && v !== "walk") {
    if (S.answer) map.show(S.answer, S.sel, { labels: { you: S.t.you_here } });
    else { map.clear(); map.mode = null; map.tourData = null; }
  }
  S.view = v;
  T.backButton(S.stack.length > 0);
  renderSheet();
}

function catLabel(cat) { return S.t.category[cat] || S.t.category.other; }
function confWord(c) { return c >= 0.75 ? S.t.confidence.high : c >= 0.5 ? S.t.confidence.mid : S.t.confidence.low; }
function host(u) { try { const h = new URL(u).hostname.replace("www.", ""); return h.includes("openstreetmap") ? "OpenStreetMap" : h.includes("2gis") ? "2ГИС" : h; } catch (_) { return u; } }
function nowLabel() {
  const at = S.answer ? S.answer.at : (S.at || serverNow().toISOString());
  const d = local(at);
  return `${S.t.day[d.getUTCDay()]} ${hm(at)}`;
}

// ---------------------------------------------------------------- top bar
function renderTop() {
  $("#q").placeholder = S.t.placeholder;
  $("#mic").setAttribute("aria-label", S.t.voice);
  $("#go").setAttribute("aria-label", S.t.send);
  const chips = S.answer ? S.answer.chips : [];
  const parts = [];
  if (!S.answer) {
    parts.push(`<button class="chip" data-act="locate">${I.pin("i-s")}${esc(S.t.locate)}</button>`);
    parts.push(`<button class="chip" data-act="machine">${I.clock("i-s")}${esc(S.t.machine)}</button>`);
    parts.push(`<button class="chip off" data-act="surprise">${I.dice("i-s")}${esc(S.t.surprise)}</button>`);
  }
  chips.forEach((c, i) => {
    const cls = c.kind === "place" ? "" : !c.active ? "off" : c.kind === "need" || c.kind === "filter" ? "on" : "";
    const icon = c.kind === "place" ? I.pin("i-s") : c.kind === "time" ? I.clock("i-s") : c.key === "kids" ? I.kid("i-s")
      : c.key === "wheelchair" ? I.wheel("i-s")
      : c.key === "only_events" ? I.event("i-s") : c.kind === "deadline" ? I.plane("i-s") : !c.active ? I.plus("i-s") : "";
    parts.push(`<button class="chip ${cls}" data-act="chip" data-i="${i}" aria-haspopup="${c.options.length ? "menu" : "false"}">${icon}${esc(c.label)}${c.assumed ? '<span class="q" aria-label="предположение">?</span>' : ""}</button>`);
  });
  $("#chips").innerHTML = parts.join("");
  const ctx = S.answer && S.answer.context;
  const wx = $("#wx");
  if (ctx && ctx.weather) {
    const w = ctx.weather, lvl = ctx.comfort ? ctx.comfort.level : 2;
    wx.hidden = false;
    wx.innerHTML = `<span class="t">${w.temp_c > 0 ? "+" : ""}${Math.round(w.temp_c)}°</span>
      <span class="d"><span>${esc(w.level)}${w.sunset ? ` · ${S.lang === "en" ? "sunset" : S.lang === "kk" ? "күн батуы" : "закат"} ${hm(w.sunset)}` : ""}</span>
      <span class="meter" role="img" aria-label="${esc(S.t.comfort)} ${lvl}/4">${[0, 1, 2, 3].map((k) => `<span class="${k < lvl ? "on" : ""}"></span>`).join("")}</span></span>`;
  } else wx.hidden = true;
}

// ---------------------------------------------------------------- sheet views (v5 design: warm paper, Lora, Suyrbek)
const ART = "assets/art/";
// a category -> its illustration (cards, the place page) and its small icon
const SPOT = { culture: "museum", food: "tea", park: "river-park", landmark: "monument", nightlife: "dombra", event: "dombra",
  kids: "kite", shopping: "monument", entertainment: "kite", sport: "river-park", leisure: "kite", work: "tea" };
const CAT_ICON = { culture: "museum", food: "cafe", park: "park", landmark: "landmark", nightlife: "events", event: "events",
  kids: "kids", entertainment: "events", sport: "park", leisure: "park", shopping: "landmark", work: "cafe" };
const TOUR_ART = { nurzhol: "esil-evening.svg", botanical: "river-park.webp", independence: "monument.webp", expo: "museum.webp" };
const QUICK_ICON = [I.pin, I.coffee, I.kid, I.wheel, I.moon, I.walk];
const U = () => UI[S.lang];
// the tab bar: four sections; the screens inside a section keep its tab lit
const TABS = ["home", "results", "tours", "saved"];
const SECTION = { home: "home", results: "results", whynot: "results", place: "results", machine: "results",
  tours: "tours", tour: "tours", saved: "saved" };
const catOf = (x) => (x && (x.kind === "event" ? "event" : x.category)) || "other";
const spot = (cat, cls = "spot") => `<img class="${cls}" src="${ART}${SPOT[cat] || "river-park"}.webp" alt="" loading="lazy" decoding="async">`;
const catIcon = (cat) => `<img class="cat" src="${ART}cat-${CAT_ICON[cat] || "landmark"}.svg" alt="" aria-hidden="true">`;
const deg = (t) => `${t > 0 ? "+" : t < 0 ? "−" : ""}${Math.abs(t)}°`;

function renderTabs() {
  const sec = SECTION[S.view], on = !!sec;            // a walk in progress and its finish keep the whole screen
  const nav = $("#tabbar");
  nav.hidden = !on;
  $("#app").classList.toggle("tabs", on);
  if (!on) return;
  const ic = { home: I.home, results: I.map, tours: I.walk, saved: I.bookmark };
  const badge = {   // what is waiting there: the picks, a walk to go on with, the saved places
    results: S.answer && S.answer.items.length && sec !== "results" ? S.answer.items.length : 0,
    tours: S.saved && S.saved.visited && S.saved.visited.length ? "dot" : 0,
    saved: S.favs.length,
  };
  nav.innerHTML = TABS.map((k) => `<button class="tab ${k === sec ? "on" : ""}" data-act="tab" data-tab="${k}"${k === sec ? ' aria-current="page"' : ""}>${ic[k]()}<span>${esc(U().tabs[k])}</span>${badge[k] === "dot" ? '<i class="n dot"></i>' : badge[k] ? `<i class="n">${badge[k]}</i>` : ""}</button>`).join("");
}

function renderSheet() {
  const v = S.view;
  let html = "";
  if (S.loading) html = viewLoading();
  else if (v === "home") html = viewHome();
  else if (v === "results") html = viewResults();
  else if (v === "whynot") html = viewWhyNot();
  else if (v === "place") html = viewPlace();
  else if (v === "machine") html = viewMachine();
  else if (v === "saved") html = viewSaved();
  else if (v === "tours") html = viewTours();
  else if (v === "tour") html = viewTour();
  else if (v === "walk") html = viewWalk();
  else if (v === "walkdone") html = viewWalkDone();
  document.body.dataset.view = v;
  $("#sheet .body").innerHTML = html;
  renderTabs();
  if (v === "machine") bindMachine();
}

const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);

function head(title, { backBtn = false, right = "", wrap = false } = {}) {
  return `<div class="view-head">${backBtn ? `<button class="icon-btn" data-act="back" aria-label="${esc(S.t.back)}">${I.back()}</button>` : ""}
    <h1 class="${wrap ? "" : "one"}">${esc(title)}</h1>${right}</div>`;
}

function mascotMood() {
  if (S.preset) return { blizzard: "cold", frost: "cold", rain: "rain", summer_evening: "summer", smog: "smog" }[S.preset] || "hello";
  const ctx = S.answer && S.answer.context;
  if (ctx && ctx.weather) return moodForWeather(ctx.weather, ctx.air);
  return moodForWeather(S.wx);
}

// clothes for the weather (frost, smog, rain, summer); null when the weather needs nothing special
function outfit() {
  const m = mascotMood();
  return ["cold", "smog", "rain", "summer"].includes(m) ? m : null;
}

// the "feels like" the mascot talks about: the simulated weather while the time machine is on, else the live one
function feelsNow() {
  const cw = S.answer && S.answer.context && S.answer.context.weather;
  if (S.preset || S.at) return cw && (!S.preset || cw.simulated) ? Math.round(cw.feels_c) : null;
  if (S.wx && S.wx.feels != null) return S.wx.feels;
  return cw ? Math.round(cw.feels_c) : null;
}

// the temperature the greeting and the time machine show, by the same rule
function tempNow() {
  const cw = S.answer && S.answer.context && S.answer.context.weather;
  if (S.preset || S.at) return cw && (!S.preset || cw.simulated) ? Math.round(cw.temp_c) : null;
  if (S.wx && S.wx.temp != null) return S.wx.temp;
  return cw ? Math.round(cw.temp_c) : null;
}

function moodLine(mood) {
  const f = feelsNow();
  const fn = W().mood[mood];
  return fn ? fn(f == null ? "" : f > 0 ? `+${f}` : String(f)) : "";
}

function mascotSays(mood, html, { size = 84 } = {}) {
  return `<div class="mascot-row">${mascot(mood, { size, title: MASCOT_NAME[S.lang] })}
    <div class="bubble"><span class="who">${esc(MASCOT_NAME[S.lang])}</span>${html}</div></div>`;
}

// "Совет Суырбека": the assistant's idea of what to do there, said by the mascot
function tip(text, { who = U().tip_who, sub = "", mood = "hello" } = {}) {
  return `<div class="tip">${mascot(mood, { size: 50, still: true })}<div><p class="tip-who">${esc(who)}</p><p>${esc(text)}</p>${sub ? `<p class="fine" style="margin-top:4px">${esc(sub)}</p>` : ""}</div></div>`;
}

function viewLoading() {
  return `<div class="thinking">${mascot("think", { size: 96, title: MASCOT_NAME[S.lang] })}<div><h2>${esc(U().looking)}</h2><p class="muted small" style="margin-top:4px">${esc(U().looking_sub)}</p></div></div>
    <div class="list" aria-label="${esc(S.t.loading)}"><div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div></div>`;
}

function walkRow(w, i) {   // a walk put together for here and now
  const first = w.answer && w.answer.items && w.answer.items[0];
  return `<button class="row-card" data-act="walk" data-i="${i}">${spot(catOf(first))}<span class="grow"><strong>${esc(w.title)}</strong>
    <span class="meta"><span>${esc(w.meta)}</span></span>${w.subtitle ? `<span class="small muted">${esc(w.subtitle)}</span>` : ""}</span>${I.chev("i-s")}</button>`;
}

function tourCard(c, wide = false) {
  const art = TOUR_ART[c.id] || "esil-evening.svg";
  return `<button class="walk-card${wide ? " wide" : ""}" data-act="tour" data-id="${esc(c.id)}"><span class="badge">${esc(W().guided)}</span>
    <span class="art"><img class="${art.endsWith(".svg") ? "wide" : ""}" src="${ART}${art}" alt="" loading="lazy" decoding="async"></span>
    <span class="walk-info"><strong>${esc(c.title)}</strong><span class="meta"><span>${esc(c.meta)}</span></span>${c.subtitle ? `<span class="s">${esc(c.subtitle)}</span>` : ""}</span></button>`;
}

function viewHome() {
  const t = S.t, u = U(), w = W();
  const mood = mascotMood(), temp = tempNow();
  const line = (mood !== "hello" && moodLine(mood)) || t.hello_sub;
  const resume = S.saved && !S.walk ? `<button class="resume" data-act="resume"><span class="resume-play">${I.play("i-s")}</span>
    <span class="grow"><strong>${esc(w.resume(S.saved.title))}</strong></span>${I.chev("i-s")}</button>` : "";
  const walks = S.walks === null ? `<section><div class="section-head"><h2>${esc(u.fits_now)}</h2></div><div class="list"><div class="skeleton"></div></div></section>`
    : S.walks && S.walks.length ? `<section><div class="section-head"><h2>${esc(u.fits_now)}</h2></div><div class="list">${S.walks.map(walkRow).join("")}</div></section>` : "";
  const tours = S.tours && S.tours.length ? `<section><div class="section-head"><h2>${esc(w.tours)}</h2><button class="link" data-act="tab" data-tab="tours">${esc(u.all)}</button></div>
    <div class="carousel">${S.tours.map((c) => tourCard(c)).join("")}</div></section>` : "";
  const invite = S.config && S.config.bot_username ? `<section class="invite"><div class="invite-top">${mascot("hello", { size: 112, still: true })}
    <div class="grow"><h2>${esc(u.invite_title)}</h2><p>${esc(u.invite_text)}</p></div></div>
    <button class="btn primary full" data-act="friends">${I.people("i-s")}${esc(u.invite_btn)}</button><p class="fine" style="text-align:center">${esc(u.invite_fine)}</p></section>` : "";
  return `<div class="greeting">${mascot(mood, { size: 120, title: MASCOT_NAME[S.lang] })}
      <div class="bubble"><h1>${esc(temp != null ? u.hello_temp(deg(temp)) : u.hello_plain)}</h1><p>${esc(line)}</p></div></div>
    ${resume}
    <div class="chip-row">${t.quick.map((q, i) => `<button class="chip" data-act="quick" data-i="${i}">${(QUICK_ICON[i] || I.pin)("i-s")}${esc(q)}</button>`).join("")}</div>
    ${walks}
    <div class="tiles"><button class="tile gold" data-act="surprise">${I.star()}<strong>${esc(t.surprise)}</strong><span>${esc(u.surprise_sub)}</span></button>
      <button class="tile" data-act="machine">${I.clock()}<strong>${esc(t.machine)}</strong><span>${esc(u.machine_sub)}</span></button></div>
    ${tours}
    ${invite}
    <button class="row-card" data-act="city"><span class="icon-tile">${I.map()}</span><span class="grow"><strong>${esc(t.city)}</strong><span class="small muted">${esc(t.city_sub)}</span></span>${I.chev("i-s")}</button>
    <div class="foot"><button class="lang-btn" data-act="lang">${esc(u.lang_label)}: <b>${esc(u.lang_name[S.lang])}</b>${I.globe("i-xs")}</button>
      <p class="privacy">${I.lock("i-xs")}${esc(t.privacy)}</p></div>`;
}

function itemCard(it, i) {
  const t = S.t, u = U(), cat = catOf(it);
  const live = it.labels.includes("now");
  const status = `<span class="status ${live ? "on" : ""}"><span class="dot ${live ? "live" : it.labels.includes("soon") ? "soon" : ""}"></span>${esc(it.status)}</span>`;
  if (i !== S.sel && S.answer.items.length > 1) {
    return `<article class="result compact" data-act="select" data-i="${i}"><span class="numb">${i + 1}</span>${catIcon(cat)}
      <span class="grow"><strong>${esc(it.title)}</strong>${status}</span><span class="mins">${it.walk_min ? `${it.walk_min} ${esc(t.min)}` : esc(t.here)}</span>
      <button class="icon-btn" data-act="route" data-i="${i}" aria-label="${esc(t.route)}">${I.route("i-s")}</button></article>`;
  }
  const walk = it.walk_min === 0 ? t.here : (it.by === "taxi" ? t.taxi_min : t.walk_min)(it.walk_min);
  const dist = it.distance_m >= 1000 ? `${(it.distance_m / 1000).toFixed(1).replace(".", ",")} км` : `${it.distance_m} ${t.m}`;
  const [lo, hi] = it.visit_min;
  const leave = it.leave_by ? (new Date(it.leave_by) <= new Date(new Date(S.answer.at).getTime() + 60000) ? t.leave_now : t.leave_by(hm(it.leave_by))) : "";
  const srcs = [...new Set((it.sources || []).map(host))].join(", ") || "OpenStreetMap";
  const facts = [
    `<span class="fact">${I.walk("i-xs")}${esc(dist)}</span>`,
    `<span class="fact">${I.clock("i-xs")}${esc(t.stay)} ${lo === hi ? lo : `${lo}–${hi}`} ${esc(t.min)}</span>`,
    it.where ? `<span class="fact">${I.pin("i-xs")}${esc(it.where)}</span>` : "",
    it.rating ? `<span class="fact gold" title="2ГИС, ${it.reviews}">${I.star("i-xs")}${String(it.rating.toFixed(1)).replace(".", ",")} · 2ГИС</span>` : "",
    leave ? `<span class="fact gold">${I.clock("i-xs")}${esc(leave)}</span>` : "",
    ...it.warnings.map((x) => `<span class="fact warn">${I.warning("i-xs")}${esc(x)}</span>`),
  ].join("");
  const why = it.pitch || it.why.join("; ");
  const todo = !it.what_to_do ? "" : it.what_to_do_source === "data"
    ? `<div class="block"><h3>${esc(u.whats_there)}<span class="src">${esc(u.from_listing)}</span></h3><p>${esc(it.what_to_do)}</p></div>`
    : tip(it.what_to_do);
  return `<article class="result sel" data-act="select" data-i="${i}">
    <div class="result-art">${spot(cat)}<span class="numb">${i + 1}</span></div>
    <div class="result-body">
      <p class="kind">${catIcon(cat)}${esc(cap(catLabel(cat)))}, ${esc(walk)}</p>
      <h2>${esc(it.title)}</h2>
      ${status}
      <div class="facts">${facts}</div>
      ${why ? `<div class="block"><h3>${esc(u.why)}</h3><p>${esc(why)}</p></div>` : ""}
      ${it.group_fit && it.group_fit.length ? `<p class="small">${it.group_fit.map(esc).join("<br>")}</p>` : ""}
      ${todo}
      <p class="source">${esc(t.source)}: ${esc(srcs)} · ${esc(confWord(it.confidence))}${it.pitch ? ` · ${esc(t.by_claude)}` : ""}</p>
      <div class="actions"><button class="btn primary" data-act="route" data-i="${i}">${I.route("i-s")}${esc(t.route)}</button>
        ${it.place_id ? `<button class="btn secondary" data-act="place" data-i="${i}">${esc(u.more)}</button>` : ""}
        <button class="icon-btn" data-act="share" data-i="${i}" aria-label="${esc(t.share)}">${I.share()}</button></div>
    </div></article>`;
}

function viewResults() {
  const a = S.answer, t = S.t, u = U();
  if (!a) return viewHome();
  const n = a.items.length;
  const f = Object.fromEntries((a.funnel || []).map((x) => [x.key, x.n]));
  const sim = a.context && a.context.simulated;
  const notes = (a.notes || []).filter((x) => !x.startsWith("Симуляция"));
  const r = a.route;
  if (!a.anchor) {   // not "nothing fits": we don't know where you are yet
    return `<div class="empty">${mascot("map", { size: 130, title: MASCOT_NAME[S.lang] })}<h1>${esc(u.where_title)}</h1><p class="muted">${esc(u.where_text)}</p></div>
      <button class="btn primary full" data-act="locate">${I.pin("i-s")}${esc(u.share_geo)}</button>
      <button class="btn secondary full" data-act="focus-search">${esc(u.type_place)}</button>`;
  }
  if (!n) {
    return `${S.stack.length ? head("", { backBtn: true }) : ""}
      <div class="empty">${mascot("shrug", { size: 130, title: MASCOT_NAME[S.lang] })}<h1>${esc(u.nothing_title)}</h1><p class="muted">${esc(notes[0] || t.nothing)}</p></div>
      <div class="list">
        <button class="option" data-act="search-more">${I.route()}<span class="grow"><strong>${esc(u.search_more)}</strong><span class="sub">${esc(u.search_more_sub)}</span></span>${I.chev("i-s")}</button>
        <button class="option" data-act="surprise">${I.star()}<span class="grow"><strong>${esc(t.surprise)}</strong><span class="sub">${esc(u.surprise_sub)}</span></span>${I.chev("i-s")}</button>
        ${a.funnel && a.funnel.length ? `<button class="option" data-act="whynot">${I.list()}<span class="grow"><strong>${esc(t.why_not)}</strong></span>${I.chev("i-s")}</button>` : ""}
      </div>
      <button class="btn secondary full" data-act="focus-search">${esc(u.change_query)}</button>`;
  }
  const plan = r ? `<div class="block"><h3>${esc(t.route_title(r.total_min))}</h3><ol class="plan">${r.stops.map((s) => `${s.walk_min ? `<li class="leg">${esc(t.walk_min(s.walk_min))}</li>` : ""}<li><b>${hm(s.arrive_at)}–${hm(s.leave_at)}</b><span>${esc(s.title)}</span></li>`).join("")}</ol></div>` : "";
  return `<div class="sheet-head"><h1>${esc(S.walkTitle || t.picked(n))}</h1><span class="when">${esc(nowLabel())}</span></div>
    ${f.nearby != null ? `<button class="why-link" data-act="whynot">${esc(u.why_link(f.nearby, n))}${I.chev("i-xs")}</button>` : ""}
    ${sim ? `<div class="sim-note">${I.clock("i-s")}<span>${esc(t.sim_banner(sim))}</span></div>` : ""}
    ${notes.length ? `<div class="note">${I.target("i-s")}<span>${notes.map(esc).join("<br>")}</span></div>` : ""}
    ${S.walkTour && S.walkTitle ? `<div class="actions"><button class="btn primary" data-act="walk-gen">${I.play("i-s")}${esc(W().start)}</button><button class="btn secondary" style="flex:0 0 auto" data-act="walk-gen-demo">${esc(W().demo)}</button></div>` : ""}
    <div class="list">${a.items.map(itemCard).join("")}</div>
    ${plan}
    <div class="feedback"><span>${esc(u.fits_q)}</span><button data-act="vote" data-v="up">${I.check("i-s")}${esc(u.yes_save)}</button><button data-act="vote" data-v="down">${I.close("i-s")}${esc(u.not_it)}</button></div>
    <div class="tiles"><button class="tile gold" data-act="surprise">${I.star()}<strong>${esc(t.surprise)}</strong><span>${esc(u.surprise_sub)}</span></button>
      <button class="tile" data-act="machine">${I.clock()}<strong>${esc(t.machine)}</strong><span>${esc(u.machine_sub)}</span></button></div>`;
}

function viewWhyNot() {
  const a = S.answer, t = S.t, u = U();
  const max = Math.max(1, ...(a.funnel || []).map((x) => x.n));
  const shades = ["#E7D3A6", "#EDC679", "#F2B33D", "#D88A2A"];
  const rows = (a.funnel || []).map((x, i, arr) => `<div class="frow ${i === arr.length - 1 ? "last" : ""}"><span>${esc(t.funnel[x.key] || x.label)}</span>
    <span class="track"><span class="bar" style="width:${Math.max(4, (x.n / max) * 100)}%;background:${i === arr.length - 1 ? "var(--pin)" : shades[Math.min(i, 3)]}"></span></span><span class="n">${x.n}</span></div>`).join("");
  const rej = (a.rejected || []).map((r, i) => `<button class="rej" data-act="rej" data-i="${i}">
    <span class="ic">${I[REASON_ICON[r.reason_code] || "target"]("i-s")}</span>
    <span class="grow"><span class="t">${esc(r.title)}</span><span class="r">${esc(r.reason)}</span></span></button>`).join("");
  return `<div class="sheet-head"><h1>${esc(t.why_not)}</h1><button class="icon-btn" data-act="back" aria-label="${esc(t.close)}">${I.close()}</button></div>
    <p class="muted" style="margin-top:-8px">${esc(u.by_facts)}</p>
    <section class="funnel" aria-label="${esc(t.why_not)}">${rows}</section>
    ${rej ? `<div><h2 style="margin-bottom:4px">${esc(u.nearby_title)}</h2>${rej}</div>` : ""}
    <div class="note">${I.shield("i-s")}<span>${esc(t.algo_note)}</span></div>`;
}

function viewPlace() {
  const p = S.place, t = S.t;
  if (!p) return "";
  const card = p.card;
  const ev = (card && card.events) || [];
  const fav = S.favs.some((f) => f.id === p.id);
  const cat = p.category || (card && card.category) || "other";
  const qa = p.qa.map((x) => `<div class="msg user">${esc(x.q)}</div>${x.a ? `<div class="msg"><b class="${x.a.certainty}">${esc(cap(t.cert[x.a.certainty]))}:</b>${esc(x.a.answer)}${x.a.evidence && x.a.evidence.length ? `<small>${esc([...new Set(x.a.evidence.map((e) => (/^https?:/.test(e) ? host(e) : e)))].join(" · "))}</small>` : ""}</div>` : `<div class="loading"><span class="spinner"></span></div>`}`).join("");
  return `<div class="place-hero">${spot(cat)}<div class="hero-tools"><button class="circle-btn" data-act="back" aria-label="${esc(t.back)}">${I.back()}</button>
      <div class="row"><button class="circle-btn ${fav ? "saved" : ""}" data-act="fav" aria-pressed="${fav}" aria-label="${esc(fav ? t.unsave : t.save)}">${I.bookmark()}</button>
        <button class="circle-btn" data-act="share-place" aria-label="${esc(t.share)}">${I.share("i-s")}</button></div></div></div>
    <div class="between" style="align-items:flex-start"><div class="block" style="gap:8px;min-width:0">
        <p class="kind">${catIcon(cat)}${esc(cap(catLabel(cat)))}${p.walk ? `, ${esc(t.walk_min(p.walk))}` : ""}</p>
        <h1 class="place-title">${esc(p.title)}</h1>
        ${card ? `<p class="live"><span class="dot"></span>${esc(card.badge)}</p>` : ""}</div>
      <span class="compass" id="compass" hidden><span id="arrow">${I.arrow("i")}</span><span class="fine" id="dist"></span></span></div>
    ${p.reason ? `<div class="note">${I.target("i-s")}<span>${esc(p.reason)}</span></div>` : ""}
    ${ev.length ? `<div class="block"><h3>${esc(t.events_here)}</h3><ul class="events">${ev.slice(0, 3).map((e) => `<li><span class="dot ${e.state === "now" ? "on" : "soon"}"></span><span class="grow">${esc(e.title)}</span><span class="when">${esc(e.badge)}</span></li>`).join("")}</ul></div>` : ""}
    <div class="actions"><button class="btn primary" data-act="route-place">${I.route("i-s")}${esc(t.route)}${p.walk ? `, ${p.walk} ${esc(t.min)}` : ""}</button>
      <button class="btn secondary" data-act="story">${I.speaker("i-s")}${esc(t.tell)}</button></div>
    ${p.story ? tip(p.story.text, { who: MASCOT_NAME[S.lang], sub: t.story_by, mood: "talk" }) : ""}
    <div class="block"><h3>${esc(t.ask_place)}</h3>
      <div class="chip-row">${t.qa_suggest.map((q, i) => `<button class="chip" data-act="qa-suggest" data-i="${i}">${esc(q)}</button>`).join("")}</div>
      ${qa ? `<div class="chat" style="margin-top:6px">${qa}</div>` : ""}
      <form class="ask-input" data-act="qa-form"><label class="sr-only" for="qa">${esc(t.ask_place)}</label>
        <input id="qa" type="text" placeholder="${esc(t.qa_placeholder)}" autocomplete="off" enterkeyhint="send">
        <button type="submit" aria-label="${esc(t.send)}">${I.send("i-s")}</button></form></div>`;
}

function serverNow() { return S.config && S.config.now ? new Date(S.config.now) : new Date(); }

function slots() {   // "now", then every half hour after it: the first tick is the current time, the next one 19:30
  const now = serverNow();
  const start = (Math.floor(now.getTime() / (30 * 60000)) + 1) * 30 * 60000;
  return [now, ...Array.from({ length: 28 }, (_, k) => new Date(start + k * 30 * 60000))];
}

function viewMachine() {
  const t = S.t, u = U();
  const sl = slots();
  const idx = S.at ? Math.max(0, sl.findIndex((d) => isoLocal(d) === S.at)) : 0;
  const lbl = S.at ? `${t.day[local(S.at).getUTCDay()]} ${hm(S.at)}` : `${t.now} · ${hm(serverNow().toISOString())}`;
  const ctx = S.answer && S.answer.context;
  const realAir = ctx && ctx.air && (ctx.air.source !== "симуляция" || S.preset === "smog");
  const mood = mascotMood(), temp = tempNow();
  const say = moodLine(mood) || (ctx && ctx.comfort ? cap(ctx.comfort.label) : t.hello_sub);
  const factors = ctx && ctx.comfort ? ctx.comfort.factors.join(" · ") + (realAir && ctx.air.pm2_5 != null ? ` · PM2.5 ${Math.round(ctx.air.pm2_5)}` : "") : "";
  return `${head(t.machine, { backBtn: true, right: `<button class="icon-btn" data-act="machine-reset" aria-label="${esc(t.reset)}" title="${esc(t.reset)}">${I.reset()}</button>` })}
    <div class="sim-note">${I.clock("i-s")}<span>${esc(u.sim_note)}</span></div>
    <div class="weather-card">${temp != null ? `<div class="wc-temp">${deg(temp)}</div>` : ""}<p class="wc-say">${esc(say)}</p>
      ${factors ? `<span class="meta"><span>${esc(factors)}</span></span>` : ""}
      ${ctx && ctx.comfort ? `<span class="meter" role="img" aria-label="${esc(t.comfort)} ${ctx.comfort.level}/4">${[0, 1, 2, 3].map((k) => `<span class="${k < ctx.comfort.level ? "on" : ""}"></span>`).join("")}</span>` : ""}
      ${mascot(mood, { size: 136, title: MASCOT_NAME[S.lang] })}</div>
    <div class="slider"><div class="between"><label for="tslider" class="muted small">${esc(u.when)}</label><output id="tlabel" for="tslider">${esc(lbl)}</output></div>
      <input id="tslider" type="range" min="0" max="${sl.length - 1}" value="${idx}">
      <div class="between fine">${[0, 7, 14, 21, 28].map((k) => `<span>${hm(sl[k].toISOString())}</span>`).join("")}</div></div>
    <div class="preset-grid" role="group" aria-label="${esc(t.weather)}">
      <button class="preset" data-act="preset" data-p="" aria-pressed="${!S.preset}">${esc(t.now)}</button>
      ${Object.entries(t.presets).map(([k, v]) => `<button class="preset" data-act="preset" data-p="${k}" aria-pressed="${S.preset === k}">${esc(v)}</button>`).join("")}</div>
    ${S.answer && S.answer.items.length ? `<button class="btn primary full" data-act="results">${I.list("i-s")}${esc(u.show_places)} · ${S.answer.items.length}</button>` : ""}`;
}

function viewSaved() {
  const t = S.t, u = U();
  return `${head(u.tabs.saved, { backBtn: S.stack.length > 0 })}
    ${S.favs.length ? `<p class="muted small" style="margin-top:-10px">${esc(u.saved_count(S.favs.length))}</p>
      <div class="list">${S.favs.map((f, i) => `<article class="saved-row">${spot(f.category || "other")}<button class="saved-open" data-act="fav-open" data-i="${i}"><strong>${esc(f.title)}</strong>
        <span class="status">${esc(cap(catLabel(f.category || "other")))}</span></button><button class="bookmark-btn" data-act="fav-del" data-i="${i}" aria-label="${esc(t.unsave)}">${I.bookmark()}</button></article>`).join("")}</div>`
      : `<div class="empty-card">${mascot("map", { size: 110, title: MASCOT_NAME[S.lang] })}<p style="margin-top:6px">${esc(t.saved_empty)}</p>
        <button class="link" data-act="tab" data-tab="results">${esc(u.find_places)}</button></div>`}`;
}

function viewTours() {
  const w = W();
  const tours = S.tours === null ? `<div class="skeleton"></div><div class="skeleton"></div>` : (S.tours || []).map((c) => tourCard(c, true)).join("");
  const near = S.walks && S.walks.length ? `<section><div class="section-head"><h2>${esc(w.nearby_walks)}</h2></div><div class="list">${S.walks.map(walkRow).join("")}</div></section>` : "";
  return `${head(w.tours, { backBtn: S.stack.length > 0 })}<div class="list">${tours}</div>${near}`;
}

// ---------------------------------------------------------------- guided walks
function viewTour() {
  const tr = S.tour, w = W(), u = U();
  if (!tr) return viewLoading();
  const resume = S.saved && S.saved.tourId === tr.id && S.saved.visited.length;
  return `${head(tr.title, { backBtn: true, wrap: true })}
    <div class="meta" style="margin-top:-8px"><span>${esc(tr.meta)}</span>${tr.subtitle ? `<span>${esc(tr.subtitle)}</span>` : ""}</div>
    ${mascotSays(outfit() || "happy", esc(tr.intro), { size: 92 })}
    <div class="actions">
      <button class="btn primary" data-act="${resume ? "resume" : "walk-start"}">${I.play("i-s")}${esc(resume ? w.resume(tr.title) : w.start)}</button>
      <button class="btn secondary" style="flex:0 0 auto" data-act="walk-demo" title="${esc(w.demo_hint)}">${esc(w.demo)}</button>
    </div>
    <div class="block"><h3>${esc(u.route_stops)}</h3><ol class="stops">${tr.stops_list.map((st, i) => `<li><button class="stop-item" data-act="tour-stop" data-i="${i}"><span class="stop-num">${st.n}</span>
      <span class="grow"><strong>${esc(st.name)}</strong><p>${esc(st.facts[0] ? st.facts[0].text : "")}</p></span></button></li>`).join("")}</ol></div>
    ${tr.hours ? `<p class="source">${esc(w.hours)}: ${esc(tr.hours.text)}${tr.hours.source ? ` <button class="src" data-act="link" data-url="${esc(tr.hours.source)}">${esc(host(tr.hours.source))}</button>` : ""}</p>` : ""}
    ${tr.best_season ? `<p class="source">${esc(w.season)}: ${esc(tr.best_season)}</p>` : ""}
    <div class="note">${I.shield("i-s")}<span>${esc(w.facts_note)} ${esc(w.privacy)}</span></div>`;
}

function stopCard(st) {
  const w = W(), k = S.walk, u = U();
  return `<article class="stop-card"><p class="kind">${I.pin("i-xs")}${esc(u.here_now)}</p><h2>${esc(st.name)}</h2>
    ${st.facts.map((f) => `<div class="fact-row"><span>${esc(f.text)}</span>${f.source ? `<button class="src" data-act="link" data-url="${esc(f.source)}">${I.link("i-xs")}${esc(f.source_name)}</button>` : `<span class="src">${esc(f.source_name)}</span>`}</div>`).join("")}
    ${st.tip ? tip(st.tip) : ""}
    ${k && k.story && k.story.stop === st.id ? tip(k.story.text, { who: MASCOT_NAME[S.lang], sub: w.story_claude, mood: "talk" }) : ""}
    <div class="actions">
      <button class="btn secondary" data-act="walk-listen">${I.speaker("i-s")}${esc(k && k.speaking ? w.stop_talk : w.listen)}</button>
      ${S.tour.kind === "tour" && S.config && S.config.llm ? `<button class="btn secondary" data-act="walk-more">${esc(w.more)}</button>` : ""}
    </div>
    <button class="btn primary full" data-act="walk-next" style="margin-top:8px">${esc(w.next)}${I.chev("i-s")}</button></article>`;
}

function viewWalk() {
  const k = S.walk, tr = S.tour, w = W();
  if (!k || !tr) return viewHome();
  const stops = tr.stops_list;
  const cur = k.current ? stops.find((x) => x.id === k.current) : null;
  const nxt = k.next ? stops.find((x) => x.id === k.next) : null;
  const total = stops.length;
  const n = Math.min(total, k.visited.length + (cur ? 0 : 1));
  const segs = stops.map((x) => `<span class="${k.visited.includes(x.id) ? "on" : x.id === k.next ? "next" : ""}"></span>`).join("");
  const dist = k.next_m != null ? w.meters(k.next_m) : "";
  const line = cur ? w.arrived(cur.name)
    : nxt ? (k.visited.length ? w.to_next(nxt.name, dist, k.next_min) : (k.next_m != null ? w.to_start(nxt.name, dist) : w.to_next(nxt.name, "", 0))) : "";
  const mood = k.speaking ? "talk" : outfit() || (cur ? "happy" : "hello");
  const nudges = (k.nudges || []).map((x) => `<div class="event-hint nudge">${x.kind === "event" ? I.music("i-s") : x.kind === "warm" ? I.snow("i-s") : I.target("i-s")}<span>${esc(x.text)}</span></div>`).join("");
  return `<div class="block" style="gap:10px"><p class="kind">${I.walk("i-xs")}${esc(tr.title)}</p>
      <div class="between" style="align-items:center"><h1>${esc(w.stop_of(n, total))}</h1><button class="link" style="color:var(--muted)" data-act="walk-finish">${esc(w.finish)}</button></div>
      <div class="walk-progress" role="progressbar" aria-valuemin="0" aria-valuemax="${total}" aria-valuenow="${k.visited.length}">${segs}</div></div>
    ${cur ? `<p class="next">${I.check("i-xs")}<span>${esc(line)}</span></p>` : mascotSays(mood, `${esc(line)}${k.off_route && nxt ? `<br><span class="warn">${esc(w.off_route(nxt.name))}</span>` : ""}`)}
    ${nudges}
    ${cur ? stopCard(cur) : ""}
    ${!cur && nxt ? `<button class="btn primary full" data-act="walk-here">${I.pin("i-s")}${esc(w.here)}</button>` : ""}
    <p class="fine">${esc(k.sim ? w.demo_hint : w.privacy)}</p>`;
}

function viewWalkDone() {
  const d = S.walkDone, w = W(), u = U();
  if (!d) return viewHome();
  const confetti = Array.from({ length: 12 }, (_, i) => `<i class="confetti" style="left:${(i * 29) % 95}%;top:${(i * 37) % 85}%;transform:rotate(${i * 29}deg);background:${["#F2B33D", "#C8412B", "#0A7BA2"][i % 3]}"></i>`).join("");
  return `<div class="done"><div class="done-art">${confetti}${mascot("celebrate", { size: 170, title: MASCOT_NAME[S.lang] })}</div>
    <div class="center"><h1>${esc(w.done_title)}</h1><p class="muted" style="margin-top:6px">${esc(d.title)}</p></div>
    <div class="stats"><div><div class="stat-v">${d.stops}</div><div class="stat-l">${esc(u.stat.stops(d.stops))}</div></div>
      <div><div class="stat-v">${d.facts}</div><div class="stat-l">${esc(u.stat.facts(d.facts))}</div></div>
      <div><div class="stat-v">${d.min}</div><div class="stat-l">${esc(u.stat.min(d.min))}</div></div></div>
    <p class="muted center">${esc(w.finish_line)}</p>
    <button class="btn primary full" data-act="walk-invite">${I.share("i-s")}${esc(w.invite)}</button>
    <button class="btn secondary full" data-act="walk-again">${esc(w.again)}</button></div>`;
}

function renderKeep() {
  const body = $("#sheet .body");
  const top = body.scrollTop;
  renderSheet();
  body.scrollTop = top;
}

function tourMapOpts(extra = {}) {
  const k = S.walk;
  return { visited: k ? k.visited : [], next: k ? k.next : null, me: k ? k.pos : null, doneLabel: W().done_label,
           onStop: (i) => focusStop(i), ...extra, mood: outfit() || "hello" };
}

function focusStop(i) {
  const st = S.tour && S.tour.stops_list[i];
  if (!st) return;
  map.focus(st.lon, st.lat);
  T.haptic.tap();
  if (S.view === "tour") document.querySelectorAll(".stop-item").forEach((r, k) => r.classList.toggle("sel", k === i));
}

async function loadTours() {
  try { S.tours = await api.tours(S.lang); } catch (_) { S.tours = []; }
  if (S.view === "home") renderSheet();
}

async function loadWx(lat, lon) {
  try { S.wx = await api.weather({ lat: lat.toFixed(2), lon: lon.toFixed(2), ...(S.at ? { at: S.at } : {}) }); } catch (_) { return; }
  if (S.view === "home" || S.view === "tour" || S.view === "walk") renderKeep();
}

async function openTour(id, { walk = null } = {}) {
  stopWalk();
  S.tour = null;
  go("tour");
  setSheet("half");
  try {
    S.tour = walk ? walk.tour : await api.tour(id, S.lang);
  } catch (_) { toast(S.t.net_error); back(); return; }
  renderSheet();
  map.showTour(S.tour, tourMapOpts());
  if (S.tour.start) loadWx(S.tour.start[0], S.tour.start[1]);
}

function startWalk({ sim = false, resume = null } = {}) {
  const tr = S.tour;
  if (!tr) return;
  stopWalk();
  const visited = resume ? resume.visited.filter((id) => tr.stops_list.some((x) => x.id === id)) : [];
  const nxt = WK.nextStop(tr.stops_list, visited);
  S.walk = { tourId: tr.id, visited, current: null, next: nxt ? nxt.id : null, next_m: null, next_min: null, off_route: false, sim,
             startedAt: resume ? resume.startedAt : Date.now(), speaking: false, nudges: [], story: null,
             facts: resume ? resume.facts || 0 : 0, lastNudgeAt: 0, pos: null, uiAt: 0, timers: [] };
  S.walkDone = null;
  if (S.view !== "walk") go("walk"); else renderSheet();
  setSheet("half");
  T.haptic.ok();
  map.showTour(tr, tourMapOpts());
  if (sim) runDemo(); else { saveWalk(); track(); }
}

function saveWalk() {
  const k = S.walk;
  if (!k || k.sim || S.tour.kind !== "tour") return;
  S.saved = { tourId: k.tourId, title: S.tour.title, visited: k.visited, startedAt: k.startedAt, facts: k.facts };
  T.store.set("walk", JSON.stringify(S.saved));
}

function stopWalk() {
  const k = S.walk;
  if (!k) return;
  if (k.watch != null && navigator.geolocation) navigator.geolocation.clearWatch(k.watch);
  clearInterval(k.poll);
  cancelAnimationFrame(k.raf);
  k.timers.forEach(clearTimeout);
  if ("speechSynthesis" in window) speechSynthesis.cancel();
  S.walk = null;
}

function track() {
  const k = S.walk;
  if (navigator.geolocation && navigator.geolocation.watchPosition) {
    k.watch = navigator.geolocation.watchPosition((p) => onPosition(p.coords.latitude, p.coords.longitude, p.coords.accuracy),
      () => pollPosition(), { enableHighAccuracy: true, maximumAge: 4000, timeout: 20000 });
  } else pollPosition();
}

function pollPosition() {
  const k = S.walk;
  if (!k || k.poll) return;
  const tick = async () => {
    const p = await T.getLocation(8000);
    if (S.walk !== k) return;
    if (p) onPosition(p.lat, p.lon, 50);
    else if (!k.warned) { k.warned = true; toast(W().geo_off, 4500); }
  };
  tick();
  k.poll = setInterval(tick, 10000);
}

// one position (real GPS or the demo walker): arrival, next stop, distance; nothing leaves the phone
function onPosition(lat, lon, acc = 0) {
  const k = S.walk;
  if (!k) return;
  k.pos = { lat, lon };
  map.setWalker(lon, lat, { follow: true });
  if (k.current) return;                 // a stop card is open: arrivals wait until "Дальше"
  const pr = WK.progress(S.tour, lat, lon, k.visited, acc);
  k.next = pr.next; k.next_m = pr.next_m; k.next_min = pr.next_min; k.off_route = pr.off_route;
  if (pr.arrived) { arrive(pr.arrived, pr.visited); return; }
  k.visited = pr.visited;
  if (pr.done) { finishWalk(); return; }
  if (Date.now() - k.uiAt > 600 && S.view === "walk") { k.uiAt = Date.now(); renderKeep(); }
  maybeNudges(lat, lon);
}

function arrive(id, visited) {
  const k = S.walk;
  const st = S.tour.stops_list.find((x) => x.id === id);
  if (!st) return;
  k.visited = [...new Set(visited)];
  k.current = id;
  k.story = null;
  k.facts += st.facts.length;
  const nxt = WK.nextStop(S.tour.stops_list, k.visited);
  k.next = nxt ? nxt.id : null;
  T.haptic.ok();
  map.showTour(S.tour, tourMapOpts({ fit: false }));
  if (S.view !== "walk") go("walk"); else renderSheet();
  if (!k.sim) setSheet("full");
  $("#sheet .body").scrollTop = 0;
  saveWalk();
  maybeNudges(st.lat, st.lon, true);   // what is on right here (an evening exhibition inside, a concert next door)
  if (k.sim) {   // the demo tells the facts aloud and walks on by itself, after enough time to read them
    const go_on = () => { if (S.walk === k && k.current === id) nextFromStop(); };
    const chars = st.facts.reduce((n, f) => n + f.text.length, 0) + (st.tip || "").length;
    const readBy = Date.now() + Math.min(13000, Math.max(5500, chars * 45));
    speakStop(st, () => k.timers.push(setTimeout(go_on, Math.max(900, readBy - Date.now()))));
    k.timers.push(setTimeout(go_on, 16000));
  }
}

function nextFromStop() {
  const k = S.walk;
  if (!k) return;
  if ("speechSynthesis" in window) speechSynthesis.cancel();
  k.speaking = false;
  k.current = null;
  if (!k.sim && S.sheet === "full") setSheet("half");
  if (!k.next) { finishWalk(); return; }
  if (k.pos) {
    const st = S.tour.stops_list.find((x) => x.id === k.next);
    const m = WK.haversine(k.pos.lat, k.pos.lon, st.lat, st.lon) * 1.3;
    k.next_m = Math.round(m); k.next_min = WK.walkMinutes(m);
  }
  renderKeep();
}

function arriveManually() {
  const k = S.walk;
  if (!k || !k.next) return;
  arrive(k.next, [...k.visited, k.next]);
}

function runDemo() {
  const k = S.walk;
  k.path = WK.demoPath(S.tour);
  k.dist = 0;
  k.pi = -1;
  k.t0 = null;
  const step = (now) => {
    if (S.walk !== k) return;
    if (k.current) { k.t0 = now; k.raf = requestAnimationFrame(step); return; }
    const dt = k.t0 == null ? 0 : (now - k.t0) / 1000;
    k.t0 = now;
    k.dist += Math.min(dt, 0.1) * DEMO_MPS;
    const i = Math.min(k.path.length - 1, Math.floor(k.dist / 6));
    if (i !== k.pi) { k.pi = i; onPosition(k.path[i].lat, k.path[i].lon, 5); }
    if (S.walk !== k) return;
    if (i >= k.path.length - 1 && !k.current) { finishWalk(); return; }
    k.raf = requestAnimationFrame(step);
  };
  k.raf = requestAnimationFrame(step);
  maybeNudges(S.tour.stops_list[0].lat, S.tour.stops_list[0].lon, true);
}

async function maybeNudges(lat, lon, force = false) {
  const k = S.walk;
  if (!k || (!force && Date.now() - k.lastNudgeAt < (k.sim ? 3500 : 60000))) return;
  k.lastNudgeAt = Date.now();
  const nx = S.tour.stops_list.find((x) => x.id === k.next);
  const q = { lat: lat.toFixed(3), lon: lon.toFixed(3), lang: S.lang, ...(S.at ? { at: S.at } : {}), ...(S.preset ? { weather_preset: S.preset } : {}),
              ...(nx ? { next_lat: nx.lat, next_lon: nx.lon } : {}) };
  try {
    const r = await api.nudges(q);
    if (S.walk !== k) return;
    if (r.weather) S.wx = r.weather;
    // one hint per event or place: a newer one (closer, fresher time) replaces the old instead of piling up
    // the hints are for where you are now: the server answers for a ~450 m circle, older ones go away
    const key = (x) => `${x.kind}:${x.event_id || x.place_id || x.text}`;
    const seen = new Set(k.nudges.map(key));
    const fresh = r.nudges.filter((x) => !seen.has(key(x)));
    const merged = r.nudges.slice(0, 2);
    const changed = JSON.stringify(merged.map((x) => x.text)) !== JSON.stringify(k.nudges.map((x) => x.text));
    k.nudges = merged;
    if (fresh.length) T.haptic.tap();
    if (changed) renderKeep();
  } catch (_) { /* hints are optional */ }
}

function speak(text, onend) {
  const k = S.walk;
  if (!("speechSynthesis" in window) || !text) { if (onend) onend(); return; }
  speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(text);
  u.lang = SPEECH_LANG[S.lang];
  let done = false;
  const end = () => { if (done) return; done = true; if (S.walk === k && k) { k.speaking = false; if (S.view === "walk") renderKeep(); } if (onend) onend(); };
  u.onend = end;
  u.onerror = end;
  if (k) { k.speaking = true; if (S.view === "walk") renderKeep(); }
  speechSynthesis.speak(u);
}

function speakStop(st, onend) {
  speak(`${st.name}. ${st.facts.map((f) => f.text).join(" ")}${st.tip ? ` ${st.tip}` : ""}`, onend);
}

async function storyAtStop() {
  const k = S.walk;
  const st = k && S.tour.stops_list.find((x) => x.id === k.current);
  if (!st) return;
  try {
    const r = await api.tourStory(S.tour.id, st.id, S.lang);
    if (S.walk !== k) return;
    k.story = { stop: st.id, text: r.text };
    renderKeep();
    speak(r.text);
  } catch (_) { toast(S.t.net_error); }
}

function finishWalk() {
  const k = S.walk;
  if (!k) return;
  const tr = S.tour;
  const share = k.visited.length / Math.max(1, tr.stops_list.length);   // the demo's minutes: the part of the tour walked
  S.walkDone = { tourId: tr.id, title: tr.title, stops: k.visited.length, facts: k.facts,
                 min: k.sim ? Math.max(1, Math.round((tr.duration_min || 1) * share)) : Math.max(1, Math.round((Date.now() - k.startedAt) / 60000)) };
  stopWalk();
  if (tr.kind === "tour") { S.saved = null; T.store.set("walk", ""); }
  map.showTour(tr, { ...tourMapOpts(), visited: tr.stops_list.map((x) => x.id), next: null, fit: true });
  S.stack = S.stack.filter((v) => v !== "walk" && v !== "walkdone");
  go("walkdone");
  T.haptic.ok();
}

async function inviteWalk() {
  const d = S.walkDone || (S.tour && { tourId: S.tour.id, title: S.tour.title });
  if (!d) return;
  const bot = S.config && S.config.bot_username;
  const url = bot ? `https://t.me/${bot}?startapp=t_${d.tourId}` : `${location.origin}${location.pathname}?tour=${encodeURIComponent(d.tourId)}`;
  const res = await T.share(url, `Qydyr: ${d.title}`);
  if (res === "copied") toast(S.t.shared);
}

// ---------------------------------------------------------------- actions
async function ask(extra = {}) {
  S.loading = true;
  S.walkTitle = null;
  if (S.view !== "results" && S.view !== "machine") go("results");
  else renderSheet();
  if (S.sheet === "low") setSheet("half");
  try {
    const body = { text: S.query, at: S.at, overrides: S.overrides, weather_preset: S.preset, lang: S.lang, ...extra };
    if (S.loc) { body.lat = S.loc.lat; body.lon = S.loc.lon; }
    const r = await api.ask(body);
    show(r.answer);
    T.haptic.ok();
  } catch (e) {
    console.error(e);
    S.loading = false;
    renderSheet();
    toast(S.t.net_error);
  }
}

function show(answer, { view } = {}) {
  S.answer = answer;
  S.sel = 0;
  S.loading = false;
  renderTop();
  if (view) go(view); else if (S.view !== "machine") { if (S.view !== "results") go("results"); else renderSheet(); } else renderSheet();
  if (!answer.anchor && S.sheet !== "full") setSheet("full");   // "Где вы?": the buttons, not the empty map
  map && map.show(answer, 0, { labels: { you: S.t.you_here } });
  fx && fx.set((answer.context && answer.context.simulated) ? S.preset : null);
}

let surprising = false;
async function surprise() {
  if (surprising) return;
  surprising = true;
  $("#q").value = "";
  T.haptic.bump();
  const wrap = document.createElement("div");
  wrap.className = "asyq-wrap";
  wrap.innerHTML = ASYQ;
  $("#app").append(wrap);
  const wait = new Promise((r) => setTimeout(r, 1150));
  S.query = "";
  try {
    const body = { surprise: true, seed: Math.floor(Math.random() * 1e6), at: S.at, weather_preset: S.preset, lang: S.lang };
    if (S.loc) { body.lat = S.loc.lat; body.lon = S.loc.lon; }
    const [r] = await Promise.all([api.ask(body), wait]);
    wrap.remove();
    S.overrides = {};
    show(r.answer, { view: S.view === "results" ? undefined : "results" });
  } catch (e) {
    wrap.remove();
    toast(S.t.net_error);
  } finally {
    surprising = false;
  }
}

function openChipMenu(btn, chip) {
  closeMenu();
  if (chip.key === "anchor") {
    chip = { ...chip, options: [{ label: S.t.locate, patch: { __locate: true } }] };
  }
  if (!chip.options.length) return;
  const m = document.createElement("div");
  m.className = "chip-menu";
  m.setAttribute("role", "menu");
  m.innerHTML = chip.options.map((o, i) => `<button role="menuitemradio" aria-checked="${o.selected ? "true" : "false"}" data-o="${i}">${esc(o.label)}${o.selected ? I.check("i-s") : ""}</button>`).join("");
  const r = btn.getBoundingClientRect();
  m.style.left = `${Math.max(8, Math.min(r.left, window.innerWidth - 220))}px`;
  m.style.top = `${r.bottom + 6}px`;
  m.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-o]");
    if (!b) return;
    const opt = chip.options[+b.dataset.o];
    closeMenu();
    T.haptic.tap();
    if (opt.patch.__locate) { locate(true); return; }
    S.overrides = merge(S.overrides, opt.patch);
    ask();
  });
  $("#app").append(m);
  armMenuClose();
}
let menuOff = null;
function armMenuClose() {
  setTimeout(() => { menuOff = (e) => { if (!e.target.closest(".chip-menu")) closeMenu(); }; document.addEventListener("click", menuOff); }, 0);
}
function closeMenu() {
  const open = document.querySelectorAll(".chip-menu");
  open.forEach((m) => m.remove());
  if (menuOff) { document.removeEventListener("click", menuOff); menuOff = null; }
  return open.length > 0;
}
function merge(ov, patch) {
  const out = { ...ov };
  for (const [k, v] of Object.entries(patch)) {
    if (k === "add_needs" || k === "remove_needs") {
      const other = k === "add_needs" ? "remove_needs" : "add_needs";
      out[k] = [...new Set([...(out[k] || []), ...v])];
      out[other] = (out[other] || []).filter((n) => !v.includes(n));
    } else out[k] = v;
  }
  return out;
}

function routeLinks(lat, lon) {
  return {
    "2ГИС": `https://2gis.kz/astana/directions/points/%7C${lon}%2C${lat}`,
    "Яндекс": `https://yandex.kz/maps/?rtext=~${lat},${lon}&rtt=pd`,
    "Google": `https://www.google.com/maps/dir/?api=1&destination=${lat},${lon}&travelmode=walking`,
  };
}

function openRoute(lat, lon, btn) {
  const links = routeLinks(lat, lon);
  openChipMenuLike(btn, Object.entries(links).map(([name, url]) => ({ label: `${S.t.open_in} ${name}`, url })));
}
function openChipMenuLike(btn, opts) {
  closeMenu();
  const m = document.createElement("div");
  m.className = "chip-menu";
  m.setAttribute("role", "menu");
  m.innerHTML = opts.map((o, i) => `<button role="menuitem" data-o="${i}">${esc(o.label)}${I.chev("i-s")}</button>`).join("");
  const r = btn.getBoundingClientRect();
  m.style.left = `${Math.max(8, Math.min(r.left, window.innerWidth - 220))}px`;
  m.style.top = `${Math.max(8, r.top - opts.length * 46 - 16)}px`;
  m.addEventListener("click", (e) => { const b = e.target.closest("button[data-o]"); if (b) { closeMenu(); T.openLink(opts[+b.dataset.o].url); } });
  $("#app").append(m);
  armMenuClose();
}

function shareText(it) {
  return `Qydyr: ${it.title} — ${it.walk_min ? (it.by === "taxi" ? S.t.taxi_min : S.t.walk_min)(it.walk_min) : S.t.here}. ${it.status}`;
}
async function shareItem(it) {
  const bot = S.config && S.config.bot_username;
  const id = S.answer && S.answer.id;
  const url = !id ? (bot ? `https://t.me/${bot}` : `${location.origin}${location.pathname}`)
    : bot ? `https://t.me/${bot}?startapp=a_${id}` : `${location.origin}${location.pathname}?answer=${id}`;
  const res = await T.share(url, shareText(it));
  if (res === "copied") toast(S.t.shared);
}

async function openPlace(src, { reason } = {}) {
  const pid = src.place_id;
  S.place = { id: pid || src.event_id || src.title, title: src.title, category: src.category, walk: src.walk_min,
              lat: src.lat, lon: src.lon, reason, card: null, qa: [], story: null };
  go("place");
  startCompass();
  if (pid) {
    try { S.place.card = await api.place(pid, S.at); renderSheet(); startCompass(); } catch (_) { /* events without a venue */ }
  }
}

let stopHeading = null;
function startCompass() {
  if (stopHeading) stopHeading();
  const p = S.place, a = S.answer && S.answer.anchor;
  if (!p || !a) return;
  const toRad = (x) => (x * Math.PI) / 180;
  const y = Math.sin(toRad(p.lon - a.lon)) * Math.cos(toRad(p.lat));
  const x = Math.cos(toRad(a.lat)) * Math.sin(toRad(p.lat)) - Math.sin(toRad(a.lat)) * Math.cos(toRad(p.lat)) * Math.cos(toRad(p.lon - a.lon));
  const bearing = ((Math.atan2(y, x) * 180) / Math.PI + 360) % 360;
  const dist = Math.round(Math.hypot((p.lat - a.lat) * 111320, (p.lon - a.lon) * 111320 * Math.cos(toRad(a.lat))));
  stopHeading = T.onHeading((heading) => {
    const c = $("#compass"), arrow = $("#arrow svg"), d = $("#dist");
    if (!c || !arrow || heading == null) return;
    c.hidden = false;
    arrow.style.transform = `rotate(${bearing - heading}deg)`;
    d.textContent = dist >= 1000 ? `${(dist / 1000).toFixed(1)} км` : `${dist} м`;
  });
}

async function askPlace(q) {
  const p = S.place;
  if (!p || !q.trim()) return;
  const entry = { q: q.trim(), a: null };
  p.qa.push(entry);
  renderSheet();
  try {
    entry.a = p.card ? await api.placeAsk(p.card.place_id, entry.q, S.at) : { answer: S.t.cert.unknown, certainty: "unknown", evidence: [] };
  } catch (_) {
    entry.a = { answer: S.t.net_error, certainty: "unknown", evidence: [] };
  }
  renderSheet();
  const body = $("#sheet .body");
  body.scrollTop = body.scrollHeight;
}

async function tellStory() {
  const p = S.place;
  if (!p || !p.card) { toast(S.t.cert.unknown); return; }
  try {
    p.story = await api.story(p.card.place_id, S.lang);
    renderSheet();
    if ("speechSynthesis" in window) {
      const u = new SpeechSynthesisUtterance(p.story.text);
      u.lang = SPEECH_LANG[S.lang];
      speechSynthesis.cancel();
      speechSynthesis.speak(u);
    }
  } catch (_) { toast(S.t.net_error); }
}

function saveFav(f) {
  if (!S.favs.some((x) => x.id === f.id)) S.favs.push(f);
  S.favs = S.favs.slice(-20);
  T.store.set("favs", JSON.stringify(S.favs));
  T.haptic.tap();
}

function toggleFav() {
  const p = S.place;
  const i = S.favs.findIndex((f) => f.id === p.id);
  if (i >= 0) { S.favs.splice(i, 1); T.store.set("favs", JSON.stringify(S.favs)); T.haptic.tap(); }
  else saveFav({ id: p.id, place_id: p.card ? p.card.place_id : null, title: p.title, lat: p.lat, lon: p.lon, category: p.category });
  renderSheet();
}

let machineTimer = null;
function bindMachine() {
  const sl = $("#tslider");
  if (!sl) return;
  const s = slots();
  sl.addEventListener("input", () => {
    const d = s[+sl.value];
    S.at = +sl.value === 0 ? null : isoLocal(d);
    $("#tlabel").textContent = S.at ? `${S.t.day[local(S.at).getUTCDay()]} ${hm(S.at)}` : `${S.t.now} · ${hm(serverNow().toISOString())}`;
    clearTimeout(machineTimer);
    machineTimer = setTimeout(() => ask(), 380);
  });
}

async function locate(explicit = false) {
  let p = await T.getLocation(explicit ? 12000 : 8000);
  if (!p) {   // Telegram's webview often has no geolocation: take the one shared with the bot in the chat
    try { const r = await api.myLocation(); if (r.found) { p = { lat: r.lat, lon: r.lon }; toast(U().loc_from_chat, 3200); } } catch (_) { /* not in Telegram */ }
  }
  if (p && !inAstana(p.lat, p.lon)) { toast(S.t.not_astana, 5200); p = null; explicit = false; }
  if (p) {
    S.loc = { lat: T.roundCoord(p.lat), lon: T.roundCoord(p.lon) };
    if (map && !["tour", "walk", "walkdone"].includes(S.view)) map.center(S.loc.lon, S.loc.lat, 14.5);   // a tour keeps its own frame
    loadWalks();
    loadWx(S.loc.lat, S.loc.lon);
    if (explicit) ask();
  } else {
    if (explicit) toast(S.t.geo_denied);
    S.walks = [];
    if (S.view === "home") renderSheet();
    if (!explicit) toast(S.t.no_geo, 4200);
  }
  renderTop();
}

function inAstana(lat, lon) { return lat > 50.95 && lat < 51.35 && lon > 71.1 && lon < 71.8; }

async function loadWalks() {
  if (!S.loc) return;
  try {
    S.walks = await api.walks({ lat: S.loc.lat, lon: S.loc.lon, lang: S.lang, ...(S.at ? { at: S.at } : {}) });
  } catch (_) { S.walks = []; }
  if (S.view === "home") renderSheet();
}

// voice input. Telegram's webview has no SpeechRecognition, so when the server can transcribe (Whisper, /voice)
// the mic records a short clip and sends it there; the browser's own recognition is the fallback elsewhere.
function setupVoice() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const canRecord = !!(window.MediaRecorder && navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
  const mic = $("#mic");
  if (!SR && !canRecord) { mic.hidden = true; return; }
  let rec = null, stopTimer = null;
  const done = () => {
    mic.classList.remove("listening");
    $("#q").placeholder = S.t.placeholder;
    clearTimeout(stopTimer);
    rec = null;
  };
  mic.addEventListener("click", async () => {
    if (rec) { rec.stop(); return; }
    T.haptic.tap();
    if (canRecord && S.config && S.config.voice) return record();
    if (!SR) { toast(S.t.voice_denied); return; }
    rec = new SR();
    rec.lang = SPEECH_LANG[S.lang];
    rec.interimResults = true;
    mic.classList.add("listening");
    $("#q").placeholder = S.t.listening;
    rec.onresult = (e) => { $("#q").value = Array.from(e.results).map((r) => r[0].transcript).join(" "); };
    rec.onend = () => { done(); if ($("#q").value.trim()) submit(); };
    rec.onerror = (e) => { toast(e.error === "not-allowed" ? S.t.voice_denied : S.t.voice_unsupported); };
    rec.start();
  });

  async function record() {
    let stream;
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); } catch (_) { toast(S.t.voice_denied); return; }
    const type = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/mp4"].find((t) => MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(t)) || "";
    const chunks = [];
    rec = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
    rec.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data); };
    rec.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop());
      done();
      const blob = new Blob(chunks, { type: (type || "audio/webm").split(";")[0] });
      if (blob.size < 1500) return;   // a tap, not a sentence
      $("#q").placeholder = S.t.listening;
      try {
        const { text } = await api.voice(blob, S.lang);
        $("#q").placeholder = S.t.placeholder;
        if (!text) { toast(S.t.voice_fail); return; }
        $("#q").value = text;
        submit();
      } catch (_) {
        $("#q").placeholder = S.t.placeholder;
        toast(S.t.voice_fail);
      }
    };
    mic.classList.add("listening");
    $("#q").placeholder = S.t.voice_stop;
    rec.start();
    stopTimer = setTimeout(() => rec && rec.stop(), 15000);   // a request, not a speech
  }
}

function submit() {
  const q = $("#q").value.trim();
  if (!q) return;
  S.query = q;
  S.overrides = {};
  $("#q").blur();
  ask();
}

function setLang(lang) {
  S.lang = lang;
  S.t = STR[lang];
  document.documentElement.lang = lang;
  T.store.set("lang", lang);
  renderTop();
  renderSheet();
  loadWalks();   // walk titles come from the server in the chosen language
  loadTours();
  if (S.tour && (S.view === "tour")) openTour(S.tour.id);
  if (S.answer) ask();
}

function applyTheme(theme) {
  S.theme = theme;
  document.documentElement.dataset.theme = theme;
  if (map && S.config) map.setStyle(S.config.map_style[theme], theme === "light" ? "#F3EBDD" : "#1B1612");
}

// ---------------------------------------------------------------- events
function bind() {
  $("#ask").addEventListener("submit", (e) => { e.preventDefault(); submit(); });
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-act]");
    if (!b) return;
    const act = b.dataset.act, i = b.dataset.i != null ? +b.dataset.i : null;
    const a = S.answer;
    switch (act) {
      case "back": back(); break;
      case "locate": locate(true); break;
      case "machine": go("machine"); setSheet("half"); break;
      case "machine-reset": S.at = null; S.preset = null; fx.set(null); ask(); break;
      case "preset": S.preset = b.dataset.p || null; T.haptic.tap(); ask(); break;
      case "results": go("results"); break;
      case "surprise": surprise(); break;
      case "saved": go("saved"); break;
      case "lang": setLang({ ru: "kk", kk: "en", en: "ru" }[S.lang]); break;
      case "quick": S.query = QUICK_QUERIES[i]; $("#q").value = S.t.quick[i]; S.overrides = {}; ask(); break;
      case "chip": e.stopPropagation(); openChipMenu(b, a.chips[i]); break;
      case "whynot": go("whynot"); setSheet("full"); break;
      case "select":
        if (e.target.closest("button")) break;
        S.sel = i; renderSheet(); map.show(a, i, { fit: false, labels: { you: S.t.you_here } }); map.focus(a.items[i].lon, a.items[i].lat); T.haptic.tap(); break;
      case "route": openRoute(a.items[i].lat, a.items[i].lon, b); break;
      case "route-place": openRoute(S.place.lat, S.place.lon, b); break;
      case "share": shareItem(a.items[i]); break;
      case "share-place": shareItem({ title: S.place.title, walk_min: S.place.walk, status: (S.place.card && S.place.card.badge) || "" }); break;
      case "place": openPlace(a.items[i]); setSheet("full"); break;
      case "rej": { const r = a.rejected[i]; map.popup(r); map.focus(r.lon, r.lat); if (r.place_id) openPlace(r, { reason: r.reason }); break; }
      case "story": tellStory(); break;
      case "fav": toggleFav(); break;
      case "fav-open": { const f = S.favs[i]; S.place = null; openPlace({ place_id: f.place_id, title: f.title, lat: f.lat, lon: f.lon, category: f.category }); break; }
      case "qa-suggest": askPlace(S.t.qa_suggest[i]); break;
      case "walk": { const w = S.walks[i]; S.walkTitle = w.title; S.walkTour = w.tour || null; show(w.answer); S.walkTitle = w.title; renderSheet(); break; }
      case "walk-gen": case "walk-gen-demo": if (S.walkTour) { S.tour = S.walkTour; startWalk({ sim: act === "walk-gen-demo" }); } break;
      case "tour": openTour(b.dataset.id); break;
      case "tour-stop": focusStop(i); break;
      case "walk-start": startWalk(); break;
      case "walk-demo": startWalk({ sim: true }); break;
      case "walk-here": arriveManually(); break;
      case "walk-next": nextFromStop(); break;
      case "walk-listen": { const k = S.walk; if (!k) break; if (k.speaking) { speechSynthesis.cancel(); k.speaking = false; renderKeep(); } else { const st = S.tour.stops_list.find((x) => x.id === k.current); if (st) speakStop(st); } break; }
      case "walk-more": storyAtStop(); break;
      case "walk-finish": {   // a real walk cut short asks first: finishing drops the saved progress
        const k = S.walk;
        if (!k || k.sim || !S.tour || k.visited.length >= S.tour.stops_list.length) { finishWalk(); break; }
        const text = W().finish_confirm;
        if (T.tg && T.tg.showConfirm) T.tg.showConfirm(text, (ok) => { if (ok) finishWalk(); });
        else if (window.confirm(text)) finishWalk();
        break;
      }
      case "walk-invite": inviteWalk(); break;
      case "walk-again": S.walkDone = null; S.stack = []; map.clear(); map.mode = null; map.tourData = null; go("home", false); T.backButton(false); break;
      case "resume": if (S.saved) { const sv = S.saved; if (S.tour && S.tour.id === sv.tourId) startWalk({ resume: sv }); else openTour(sv.tourId).then(() => S.tour && startWalk({ resume: sv })); } break;
      case "link": T.openLink(b.dataset.url); break;
      case "friends": T.openLink(`https://t.me/${S.config.bot_username}?startgroup=go`); break;
      case "city": T.openLink(new URL("city.html", location.href).href); break;
      case "vote": {   // "Да, сохранить" also keeps the picked place
        api.feedback(a.id, b.dataset.v).catch(() => {});
        const it = a.items[S.sel];
        if (b.dataset.v === "up" && it) { saveFav({ id: it.place_id || it.event_id || it.title, place_id: it.place_id || null, title: it.title, lat: it.lat, lon: it.lon, category: catOf(it) }); toast(U().saved_ok(it.title)); }
        else toast(S.lang === "ru" ? "Спасибо, учту" : S.lang === "kk" ? "Рақмет" : "Thanks");
        break;
      }
      case "fav-del": S.favs.splice(i, 1); T.store.set("favs", JSON.stringify(S.favs)); T.haptic.tap(); renderSheet(); break;
      case "search-more": S.overrides = merge(S.overrides, { budget_min: 60, budget_max: 90 }); ask(); break;
      case "focus-search": $("#q").focus(); break;
      case "tab": {   // a section, not a step: the stack starts over
        const v = b.dataset.tab;
        T.haptic.tap();
        if (SECTION[S.view] === v && v !== "tours") { $("#sheet .body").scrollTo({ top: 0, behavior: "smooth" }); if (S.view !== v) { S.stack = []; go(v, false); } break; }
        if (v === "tours" && S.walk) { go("walk", false); break; }   // a walk in progress: back to it
        S.stack = [];
        if (v === "results" && !S.answer) { S.query = QUICK_QUERIES[0]; $("#q").value = S.t.quick[0]; S.overrides = {}; S.view = "home"; ask(); break; }
        go(v, false);
        break;
      }
      default: break;
    }
  });
  document.addEventListener("submit", (e) => {
    if (e.target.matches("[data-act=qa-form]")) { e.preventDefault(); const inp = $("#qa"); askPlace(inp.value); inp.value = ""; }
  });
  // the keyboard is up while typing: hide the tab bar so it doesn't ride on it
  document.addEventListener("focusin", (e) => { if (e.target.matches("input[type=text]")) document.body.classList.add("typing"); });
  document.addEventListener("focusout", (e) => { if (e.target.matches("input[type=text]")) document.body.classList.remove("typing"); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !closeMenu()) back(); });

  // the sheet: drag the handle between low / half / full
  const sheet = $("#sheet"), grab = $("#sheet .grab");
  let y0 = null, h0 = 0;
  grab.addEventListener("pointerdown", (e) => { y0 = e.clientY; h0 = sheet.getBoundingClientRect().top; sheet.classList.add("dragging"); grab.setPointerCapture(e.pointerId); });
  grab.addEventListener("pointermove", (e) => {
    if (y0 == null) return;
    const H = $("#app").clientHeight;
    const top = Math.max(H * 0.12, Math.min(H * 0.8, h0 + (e.clientY - y0)));
    sheet.style.setProperty("--peek", `${H - top}px`);
  });
  grab.addEventListener("pointerup", (e) => {
    sheet.classList.remove("dragging");
    const moved = Math.abs(e.clientY - y0);
    const frac = 1 - sheet.getBoundingClientRect().top / $("#app").clientHeight;
    y0 = null;
    if (moved < 6) { setSheet(S.sheet === "full" ? "half" : "full"); return; }
    setSheet(frac > 0.66 ? "full" : frac > 0.34 ? "half" : "low");
  });
}

// ---------------------------------------------------------------- start
async function start() {
  const saved = await T.store.get("lang");
  if (saved && STR[saved]) { S.lang = saved; S.t = STR[saved]; document.documentElement.lang = saved; }
  try { S.favs = JSON.parse((await T.store.get("favs")) || "[]"); } catch (_) { S.favs = []; }
  S.theme = "light";   // the warm paper reads best, also in Telegram's dark mode
  document.documentElement.dataset.theme = S.theme;
  $("#logo").innerHTML = I.search();
  $("#mic").innerHTML = I.mic();
  $("#go").innerHTML = I.send();
  T.initTelegram({ onBack: back, theme: S.theme });
  setSheet("full");
  renderTop();
  renderSheet();
  bind();
  setupVoice();
  fx = new WeatherFx($("#fx"));
  try { S.config = await api.config(); } catch (_) { S.config = { map_style: { dark: "", light: "" }, attribution: ["© OpenStreetMap contributors"] }; }
  renderSheet();                      // the invite card needs the bot's name from the config
  map = new CityMap($("#map"), {
    styleUrl: S.config.map_style[S.theme] || { version: 8, sources: {}, layers: [] },
    bg: S.theme === "light" ? "#F3EBDD" : "#1B1612",
    attribution: (S.config.attribution || []).join(" · "),
    onPick: (i) => { S.sel = i; if (S.view !== "results") go("results"); else renderSheet(); map.show(S.answer, i, { fit: false, labels: { you: S.t.you_here } }); T.haptic.tap(); },
    onReject: (i) => { if (S.view !== "whynot") go("whynot"); document.querySelectorAll(".rej").forEach((r, k) => r.classList.toggle("sel", k === i)); },
  });
  T.onShake(() => { if (!S.walk) surprise(); });
  try { const sv = JSON.parse((await T.store.get("walk")) || "null"); if (sv && sv.tourId) S.saved = sv; } catch (_) { S.saved = null; }
  loadTours();
  const sp = T.startParam();
  const tourId = sp.startsWith("t_") ? sp.slice(2) : new URLSearchParams(location.search).get("tour");
  const id = sp.startsWith("a_") ? sp.slice(2) : sp.startsWith("t_") ? "" : sp;
  if (tourId) openTour(tourId);
  else if (id) {
    try { show(await api.answer(id)); } catch (_) { /* expired: start fresh */ }
  }
  locate(false);
}

start();
