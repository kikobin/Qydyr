// "Qydyr для города": where typical requests find nothing nearby (the supply audit), what people look for and
// do not find (live demand, counts only), and where the data comes from. Written for a city official, not a dev:
// every number comes with what it means.
import { api } from "./api.js";
import { I, LOGO } from "./icons.js";
import { ASTANA } from "./map.js";
import * as maplibregl from "./vendor/maplibre-gl.mjs";

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const n0 = (n) => (n ?? 0).toLocaleString("ru-RU");
const pct = (a, b) => (b ? (a < b ? Math.min(99, Math.round((100 * a) / b)) : 100) : 0);   // 1 gap is never "100%"
// 1 место, 2 места, 5 мест
const plural = (n, [one, few, many]) => {
  const d = n % 10, h = n % 100;
  return d === 1 && h !== 11 ? one : d >= 2 && d <= 4 && (h < 12 || h > 14) ? few : many;
};
const KV = ["квартал", "квартала", "кварталов"];
const PL = ["место", "места", "мест"];
const COLORS = { none: "#C8402B", guess: "#7E5BC2", far: "#D99A2B", ok: "#00788E" };
const LEVEL = {
  none: ["ничего нет", "ни одного подходящего открытого места в 15 минутах"],
  far: ["только далеко", "ближайшее подходящее — дальше 15 минут пешком"],
  guess: ["не подтверждено", "есть места, которые «вероятно» подходят, но в данных это не отмечено"],
  ok: ["есть рядом", "хотя бы одно подходящее место в 15 минутах"],
};
const ICON = { kids_evening: "kid", work_quiet: "coffee", step_free: "wheel", night: "moon", frost: "snow", events: "event" };
const WEEKDAY = ["в воскресенье", "в понедельник", "во вторник", "в среду", "в четверг", "в пятницу", "в субботу"];
const SRC_KIND = { osm: "карта", catalog: "справочник", listing: "афиша", page: "сайт площадки", telegram: "Telegram", article_index: "подборки", manual: "вручную" };
const NEED = { coffee: "кофе", food: "поесть", socket: "розетка", wifi: "Wi‑Fi", quiet: "тихо", kids: "с ребёнком", playground: "площадка",
  interactive: "интерактив", museum: "музей", culture: "культура", park: "парк", walk: "прогулка", view: "красивый вид", unusual: "необычное",
  night: "ночью", nightlife: "бар или клуб", free: "бесплатно", indoor: "под крышей", accessible: "без ступенек", romantic: "свидание",
  photo: "фото", shopping: "шопинг", event: "события", cheap: "недорого", dog: "с собакой" };

let map = null, gaps = null, scenarios = [], current = null, popup = null;

// geohash -> [west, south, east, north]
const BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz";
function bbox(gh) {
  const lat = [-90, 90], lon = [-180, 180];
  let even = true;
  for (const c of gh) {
    const cd = BASE32.indexOf(c);
    for (const mask of [16, 8, 4, 2, 1]) {
      const r = even ? lon : lat, mid = (r[0] + r[1]) / 2;
      if (cd & mask) r[0] = mid; else r[1] = mid;
      even = !even;
    }
  }
  return [lon[0], lat[0], lon[1], lat[1]];
}
const cellPoly = (gh) => { const [w, s, e, n] = bbox(gh); return [[[w, s], [e, s], [e, n], [w, n], [w, s]]]; };
const cellCenter = (gh) => { const [w, s, e, n] = bbox(gh); return [(w + e) / 2, (s + n) / 2]; };
const local = (iso) => new Date(new Date(iso).getTime() + 5 * 3600000);   // Astana, UTC+5
const when = (iso) => { if (!iso) return ""; const d = local(iso); return `${WEEKDAY[d.getUTCDay()]} в ${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`; };
const ago = (iso) => {
  if (!iso) return "ещё не запускался";
  const h = Math.round((Date.now() - new Date(iso).getTime()) / 3600000);
  return h < 1 ? "меньше часа назад" : h < 48 ? `${h} ч назад` : `${Math.round(h / 24)} дн назад`;
};
const icon = (name, cls = "i") => (I[name] ? I[name](cls) : "");

const where = (r) => (r.label ? (/^(у|около) «/.test(r.label) ? `Квартал ${r.label}` : `Район «${r.label}»`) : "Квартал без названия");
function gapLine(r) {
  if (r.level === "none") return "рядом ничего подходящего";
  if (r.level === "guess") return `только «вероятно»: ${r.best.map((b) => `«${b}»`).join(", ")}`;
  if (r.level === "far") return `ближайшее — ${r.nearest_min} мин пешком${r.best.length ? `: «${r.best[0]}»` : ""}`;
  return `${r.nearest_min} мин пешком${r.best.length ? `: ${r.best.map((b) => `«${b}»`).join(", ")}` : ""}`;
}
const segs = (k, cls = "seg") => `<div class="${cls}" aria-hidden="true">${["ok", "far", "guess", "none"].map((lv) => k[lv]
  ? `<span style="width:${(100 * k[lv]) / k.cells}%;background:${COLORS[lv]}${lv === "ok" ? ";opacity:.55" : ""}"></span>` : "").join("")}</div>`;

// ---------------------------------------------------------------- the audit
function renderScenarios() {
  $("#scen").innerHTML = scenarios.map((s) => {
    const k = s.summary;
    const body = k && k.cells
      ? `<span class="pct">${pct(k.ok, k.cells)}%<small>есть рядом</small></span>${segs(k)}<span class="bad">${k.none + k.far + k.guess
        ? `${k.none + k.far + k.guess} ${plural(k.none + k.far + k.guess, KV)} без ответа рядом` : "везде есть ответ"}</span>`
      : `<span class="bad">аудит ещё не запускался</span>`;
    return `<button type="button" class="sc" data-s="${s.id}" aria-pressed="${s.id === current}"><span class="t">${icon(ICON[s.id])}${esc(s.title)}</span>${body}</button>`;
  }).join("");
}

function headline() {
  // the hero says the one finding worth remembering: the situation the city answers worst
  const worst = scenarios.filter((s) => s.summary && s.summary.cells)
    .map((s) => ({ s, bad: s.summary.none + s.summary.far + s.summary.guess }))
    .sort((a, b) => b.bad / b.s.summary.cells - a.bad / a.s.summary.cells)[0];
  if (!worst || !worst.bad) return;
  const k = worst.s.summary;
  $("#lead").innerHTML = `Мы задаём Qydyr обычные вопросы жителей из каждого квартала города и проверяем по реальным данным, есть ли ответ <b>в 15 минутах пешком</b>.
    Хуже всего город отвечает на «${esc(worst.s.title.toLowerCase())}»: <b>в ${worst.bad} из ${k.cells} ${plural(k.cells, KV)}</b> рядом ничего подходящего.`;
}

function renderSide() {
  const g = gaps, s = g.scenario, k = g.summary;
  if (!k.cells) {
    $("#side").innerHTML = `<div class="card"><span class="empty">Аудит для этой ситуации ещё не запускался. Он считается раз в сутки после сбора данных.</span></div>`;
    return;
  }
  const bad = k.none + k.guess + k.far;
  const row = (lv) => (lv === "guess" && !k.guess ? "" : `<div class="row"><i class="dot" style="background:${COLORS[lv]}${lv === "ok" ? ";opacity:.55" : ""}"></i>
    <span>${LEVEL[lv][0]}<small>${LEVEL[lv][1]}</small></span><em>${k[lv]}</em></div>`);
  $("#side").innerHTML = `
    <div class="card">
      <h3>Вопрос жителя</h3>
      <p class="quote">«${esc(s.request)}»</p>
      <div class="meta"><span class="pill">${icon("clock")}${esc(when(s.at))}</span><span class="pill">${icon("walk")}до 15 мин пешком</span></div>
      <p class="why">${esc(s.why)}</p>
    </div>
    <div class="card">
      <h3>Результат</h3>
      <div class="big"><b>${pct(k.ok, k.cells)}%</b><span>кварталов с ответом рядом.<br>${bad ? `${bad} из ${k.cells} — без ответа.` : "Пробелов нет."}</span></div>
      ${segs(k, "seg lg")}
      <div class="rows">${row("none")}${row("far")}${row("guess")}${row("ok")}</div>
    </div>
    <div class="card">
      <h3>Где хуже всего</h3>
      <div class="worst">${g.top.length ? g.top.map((r, i) => `<button type="button" class="w" data-cell="${r.cell}">
          <span class="k" style="background:${COLORS[r.level]}">${i + 1}</span>
          <span><b>${esc(where(r))}</b><span>${esc(gapLine(r))} · ${r.places} ${plural(r.places, PL)} в квартале</span></span></button>`).join("")
        : `<span class="empty">Во всех кварталах есть подходящее место рядом.</span>`}</div>
    </div>`;
}

function drawCells() {
  if (!map || !map.getSource("gaps") || !gaps) return;
  map.getSource("gaps").setData({ type: "FeatureCollection", features: gaps.cells.map((r) => ({
    type: "Feature", properties: { cell: r.cell, color: COLORS[r.level], op: r.level === "ok" ? 0.16 : 0.55 },
    geometry: { type: "Polygon", coordinates: cellPoly(r.cell) } })) });
}

function focusCell(cell, { fly = true } = {}) {
  if (!map) return;
  const r = gaps && gaps.cells.find((x) => x.cell === cell);
  const at = r ? [r.lon, r.lat] : cellCenter(cell);
  if (fly) {
    map.easeTo({ center: at, zoom: 14, duration: 600 });
    document.querySelector(".map-card").scrollIntoView({ behavior: "smooth", block: "center" });
  }
  if (popup) popup.remove();
  const html = r
    ? `<b>${esc(where(r))}</b><span style="color:${COLORS[r.level]};font-weight:600">${LEVEL[r.level][0]}</span> · ${esc(gapLine(r))}<br><span style="color:var(--muted)">${r.places} ${plural(r.places, PL)} в квартале</span>`
    : `<b>Этот квартал</b>`;
  popup = new maplibregl.Popup({ closeButton: false, offset: 6 }).setLngLat(at).setHTML(html).addTo(map);
}

async function loadGaps(id) {
  if (!id) return;
  current = id;
  renderScenarios();
  try { gaps = await api.cityGaps(id); } catch (_) { $("#side").innerHTML = `<div class="card"><span class="empty">Не удалось загрузить аудит.</span></div>`; return; }
  renderSide();
  drawCells();
  if (popup) popup.remove();
  try { history.replaceState(null, "", `?s=${id}`); } catch (_) { /* file:// */ }
}

// ---------------------------------------------------------------- live demand
async function loadDemand() {
  let d;
  try { d = await api.cityDemand(); } catch (_) { return; }
  $("#k-note").textContent = `Квартал попадает сюда, когда в нём спросили хотя бы ${d.k} раза: так нельзя вычислить конкретного человека.`;
  $("#needs").innerHTML = d.needs.length ? d.needs.slice(0, 8).map((x) => `<div class="bar"><span>${esc(NEED[x.need] || x.label)}</span>
      <span class="track" title="не нашли ${x.unmet} из ${x.asked}"><span style="width:${pct(x.unmet, x.asked)}%;background:var(--c-none)"></span><span style="width:${100 - pct(x.unmet, x.asked)}%;background:var(--c-ok);opacity:.3"></span></span>
      <em>не нашли ${x.unmet} из ${x.asked}</em></div>`).join("")
    : `<span class="empty">Пока все частые просьбы находят ответ. Как только в каком-то квартале ${d.k} раза не найдут одно и то же, это появится здесь.</span>`;
  $("#live").innerHTML = d.live.length ? d.live.slice(0, 6).map((x) => `<button type="button" class="live-row" data-cell="${x.cell}">
      <b>${esc(NEED[x.need] || x.need)}</b><span>${esc(x.label ? where(x) : "квартал на карте")} · ${x.n} ${plural(x.n, ["раз", "раза", "раз"])} не нашли</span></button>`).join("")
    : `<span class="empty">За последние 3 часа все запросы нашли ответ.</span>`;
}

// ---------------------------------------------------------------- the data
async function loadData() {
  let d;
  try { d = await api.cityData(); } catch (_) { return; }
  const p = d.places, e = d.events;
  const feeds = d.sources.filter((s) => s.kind !== "osm" && s.kind !== "catalog" && s.items).length;
  const kpis = [
    ["pin", n0(p.n), `${plural(p.n, PL)} на карте: OpenStreetMap${p.dgis_new ? ` и ещё ${n0(p.dgis_new)} из 2ГИС` : ""}`],
    ["event", n0(e.n), `${plural(e.n, ["событие", "события", "событий"])} из ${feeds} афиш и сайтов площадок`],
    ["clock", `${pct(p.with_hours, p.n)}%`, "мест с часами работы: Qydyr знает, открыто ли сейчас"],
    ["star", n0(p.with_rating), `${plural(p.with_rating, PL)} с рейтингом и отзывами 2ГИС`],
    ["wheel", `${pct(p.with_wheelchair, p.n)}%`, "мест с отметкой, можно ли проехать на коляске"],
    ["flag", n0(d.tours.facts), `${plural(d.tours.facts, ["факт", "факта", "фактов"])} в ${d.tours.n} прогулках с гидом, у каждого — ссылка на источник`],
  ];
  $("#kpis").innerHTML = kpis.map(([ic, b, s]) => `<div class="kpi"><span class="ic">${icon(ic)}</span><div><b>${esc(b)}</b><span>${esc(s)}</span></div></div>`).join("");
  const bars = [
    ["Часы работы", "чтобы не вести в закрытое", p.with_hours, p.n],
    ["Рейтинг 2ГИС", "чтобы советовать хорошее", p.with_rating || 0, p.n],
    ["Доступность", "для колясок и родителей", p.with_wheelchair, p.n],
    ["Телефон или сайт", "чтобы уточнить", p.with_contacts, p.n],
    ["Событие на карте", "знаем, где оно идёт", e.with_place + e.geocoded, e.n],
    ["Дата с цитатой", "проверено по источнику", e.evidence_ok, e.n],
  ];
  $("#coverage").innerHTML = bars.map(([l, sub, a, b]) => {
    const v = pct(a, b);
    return `<div class="bar"><span>${esc(l)}<small>${esc(sub)}</small></span><span class="track"><span style="width:${v}%;background:${v < 30 ? "var(--c-none)" : v < 60 ? "var(--c-far)" : "var(--c-ok)"}"></span></span><em>${v}%</em></div>`;
  }).join("");
  $("#cov-note").textContent = `Вручную уточнено ${d.curated.places} ${plural(d.curated.places, PL)}: как их называют люди («Чупа-чупс», «Пирамида»).`
    + (p.dgis_enriched + p.dgis_new ? ` Из 2ГИС: часы ${n0(p.hours_2gis)} мест, доступность ${n0(p.wheelchair_2gis)}, Wi‑Fi ${n0(p.wifi_2gis)}.` : "")
    + ` События: ${e.jsonld} из разметки сайтов, ${e.llm} прочитал Claude, ${e.telegram} из Telegram.`;
  const SCHED_IC = ["map", "event", "target"];
  $("#schedule").innerHTML = `<ul class="sched">${d.schedule.map((s, i) => `<li><span class="ic">${icon(SCHED_IC[i] || "clock")}</span><span><b>${esc(s.what)}</b><small>${esc(s.when)}</small></span></li>`).join("")}
    <li><span class="ic">${icon("sun")}</span><span><b>Погода и воздух</b><small>${esc(d.weather)}</small></span></li></ul>
    <p class="note">Сейчас: места обновлены ${esc(ago(p.fetched_at))}, события ${esc(ago(e.last_seen_at))}.</p>`;
  const live = d.sources.filter((s) => s.items), idle = d.sources.filter((s) => !s.items);
  $("#src-sum").textContent = `${live.length} работают${idle.length ? ` · ${idle.length} пока без данных` : ""}`;
  const row = (s) => `<div class="src-row"><span>${s.url ? `<a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title)}</a>` : esc(s.title)}<span class="tag">${esc(SRC_KIND[s.kind] || s.kind)}</span></span>
      <b>${n0(s.items)} ${esc(s.unit.split(":")[0])}</b><small>обновлено ${esc(ago(s.last_run_at))}${s.status && s.status.startsWith("error") ? ' · <span class="err">ошибка при последнем сборе</span>' : ""}</small></div>`;
  $("#sources").innerHTML = live.map(row).join("") + (idle.length ? `<p class="note">Пока без событий: ${idle.map((s) => esc(s.title)).join(", ")}.</p>` : "");
}

function renderPrivacy() {
  $("#priv").innerHTML = [
    ["shield", "Аудит — без людей", "Это вопросы, которые Qydyr задаёт сам. Личных данных в нём нет."],
    ["lock", "Спрос — только счётчики", "День, квартал ~1 км и тип просьбы. Текст, точные координаты и аккаунты не храним."],
    ["people", "Никого не выделить", "Квартал показываем, только когда в нём спросили хотя бы 3 раза. «Последние 3 часа» живут лишь в памяти сервера."],
    ["warning", "Без додумывания", "Если в данных не отмечено, что место открыто или доступно, мы так и пишем: «неизвестно» или «вероятно»."],
  ].map(([ic, b, t]) => `<div>${icon(ic)}<b>${esc(b)}</b>${esc(t)}</div>`).join("");
}

// ---------------------------------------------------------------- start
async function start() {
  $("#logo").innerHTML = LOGO;
  renderPrivacy();
  let cfg = { map_style: { light: "" } };
  try { cfg = await api.config(); } catch (_) { /* offline */ }
  map = new maplibregl.Map({ container: "cmap", style: cfg.map_style.light || { version: 8, sources: {}, layers: [] }, center: ASTANA, zoom: 11.2,
                             attributionControl: false, cooperativeGestures: true });
  map.addControl(new maplibregl.AttributionControl({ compact: true, customAttribution: "© OpenStreetMap contributors" }));
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  // no basemap (blocked CDN, venue Wi-Fi): the cells on a plain background instead of an empty map
  let styled = false;
  const plain = () => { if (!styled) { styled = true; map.setStyle({ version: 8, sources: {}, layers: [{ id: "bg", type: "background", paint: { "background-color": "#EDE3D2" } }] }); } };
  const fallback = setTimeout(plain, 3500);
  map.on("error", () => { if (!styled) plain(); });
  map.on("style.load", () => {
    styled = true;
    clearTimeout(fallback);
    if (!map.getSource("gaps")) {
      map.addSource("gaps", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      map.addLayer({ id: "gaps-fill", type: "fill", source: "gaps", paint: { "fill-color": ["get", "color"], "fill-opacity": ["get", "op"] } });
      map.addLayer({ id: "gaps-line", type: "line", source: "gaps", paint: { "line-color": ["get", "color"], "line-opacity": 0.7, "line-width": 1 } });
    }
    drawCells();
  });
  map.on("click", "gaps-fill", (ev) => focusCell(ev.features[0].properties.cell, { fly: false }));
  map.on("mouseenter", "gaps-fill", () => { map.getCanvas().style.cursor = "pointer"; });
  map.on("mouseleave", "gaps-fill", () => { map.getCanvas().style.cursor = ""; });

  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-s]");
    if (b) loadGaps(b.dataset.s);
    const g = e.target.closest("[data-cell]");
    if (g) focusCell(g.dataset.cell);
  });

  try { scenarios = await api.cityScenarios(); } catch (_) { scenarios = []; }
  headline();
  const wanted = new URLSearchParams(location.search).get("s");
  await Promise.all([loadGaps(scenarios.some((s) => s.id === wanted) ? wanted : (scenarios[0] || {}).id), loadDemand(), loadData()]);
  setInterval(loadDemand, 15000);
}

start();
