// "Qydyr для города": where typical requests find nothing nearby (the supply audit), what people look for and
// do not find (live demand, counts only), and where the data comes from.
import { api } from "./api.js";
import { LOGO } from "./icons.js";
import { ASTANA } from "./map.js";
import * as maplibregl from "./vendor/maplibre-gl.mjs";

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const n0 = (n) => (n ?? 0).toLocaleString("ru-RU");
const pct = (a, b) => (b ? Math.round((100 * a) / b) : 0);
// 1 место, 2 места, 5 мест
const plural = (n, [one, few, many]) => {
  const d = n % 10, h = n % 100;
  return d === 1 && h !== 11 ? one : d >= 2 && d <= 4 && (h < 12 || h > 14) ? few : many;
};
const COLORS = { none: "#FF7A7A", guess: "#B79CFF", far: "#FEC50C", ok: "#00AFCA" };
const WEEKDAY = ["вс", "пн", "вт", "ср", "чт", "пт", "сб"];
const SRC_KIND = { osm: "карта", catalog: "справочник", listing: "афиша", page: "сайт площадки", telegram: "Telegram", article_index: "подборки", manual: "вручную" };

let map = null, gaps = null, scenarios = [], current = null;

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
const local = (iso) => new Date(new Date(iso).getTime() + 5 * 3600000);   // Astana, UTC+5
const when = (iso) => { if (!iso) return ""; const d = local(iso); return `${WEEKDAY[d.getUTCDay()]} ${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`; };
const ago = (iso) => {
  if (!iso) return "не запускался";
  const h = Math.round((Date.now() - new Date(iso).getTime()) / 3600000);
  return h < 1 ? "меньше часа назад" : h < 48 ? `${h} ч назад` : `${Math.round(h / 24)} дн назад`;
};

function where(r) { return r.label ? `«${r.label}»` : `ячейка ${r.cell}`; }
function gapLine(r) {
  if (r.level === "none") return "ни одного подходящего варианта рядом";
  if (r.level === "guess") return `только «вероятно»: ${r.best.map((b) => `«${b}»`).join(", ")} — в данных не отмечено`;
  return `ближайший вариант — ${r.nearest_min} мин пешком${r.best.length ? `: «${r.best[0]}»` : ""}`;
}

// ---------------------------------------------------------------- the audit
function renderScenarios() {
  $("#scen").innerHTML = scenarios.map((s) => `<button type="button" data-s="${s.id}" aria-pressed="${s.id === current}">${esc(s.title)}</button>`).join("");
}

function renderSide() {
  const g = gaps, s = g.scenario, k = g.summary;
  if (!k.cells) {
    $("#side").innerHTML = `<span class="empty">Аудит ещё не запускался: <code>python -m qydyr_data audit</code></span>`;
    return;
  }
  const seg = (lv) => k[lv] ? `<span style="width:${(100 * k[lv]) / k.cells}%;background:${COLORS[lv]}${lv === "ok" ? ";opacity:.55" : ""}" title="${lv}"></span>` : "";
  const bad = k.none + (k.guess || 0) + k.far;
  $("#side").innerHTML = `
    <h3>Запрос</h3>
    <p class="req"><q>${esc(s.request)}</q><br><span class="when">${esc(when(s.at))} · из центра каждой ячейки · ${k.cells} ${plural(k.cells, ["ячейка", "ячейки", "ячеек"])}</span></p>
    <p class="why">${esc(s.why)}</p>
    <div class="stack" role="img" aria-label="${bad} из ${k.cells} ячеек без близкого подтверждённого ответа">${seg("none")}${seg("guess")}${seg("far")}${seg("ok")}</div>
    <div class="counts"><span><b>${k.none}</b> ничего рядом</span>${k.guess ? `<span><b>${k.guess}</b> только «вероятно»</span>` : ""}<span><b>${k.far}</b> дальше 15 мин</span><span><b>${k.ok}</b> есть рядом</span></div>
    <h3>Где хуже всего</h3>
    ${g.top.length ? g.top.map((r) => `<div class="gap ${r.level}" data-cell="${r.cell}" role="button" tabindex="0"><b>${esc(where(r))}</b><span>${r.places} ${plural(r.places, ["место", "места", "мест"])} в ячейке · ${esc(gapLine(r))}</span></div>`).join("")
      : `<span class="empty">Во всех ячейках есть близкий ответ.</span>`}`;
}

function drawCells() {
  if (!map || !map.getSource("gaps") || !gaps) return;
  map.getSource("gaps").setData({ type: "FeatureCollection", features: gaps.cells.map((r) => ({
    type: "Feature", properties: { cell: r.cell, color: COLORS[r.level], op: r.level === "ok" ? 0.18 : 0.62 },
    geometry: { type: "Polygon", coordinates: cellPoly(r.cell) } })) });
}

function focusCell(cell) {
  const r = gaps.cells.find((x) => x.cell === cell);
  if (!r || !map) return;
  map.easeTo({ center: [r.lon, r.lat], zoom: 14, duration: 600 });
  new maplibregl.Popup({ closeButton: false }).setLngLat([r.lon, r.lat])
    .setHTML(`<b>${esc(where(r))}</b><br>${r.places} ${plural(r.places, ["место", "места", "мест"])} в ячейке<br>${esc(r.level === "ok" ? `подходит: ${r.best.map((b) => `«${b}»`).join(", ")} · ${r.nearest_min} мин` : gapLine(r))}`)
    .addTo(map);
}

async function loadGaps(id) {
  current = id;
  renderScenarios();
  try { gaps = await api.cityGaps(id); } catch (_) { $("#side").innerHTML = `<span class="empty">Не удалось загрузить аудит.</span>`; return; }
  renderSide();
  drawCells();
}

// ---------------------------------------------------------------- live demand
const NEED = { coffee: "кофе", food: "поесть", socket: "розетка", wifi: "Wi‑Fi", quiet: "тихо", kids: "с ребёнком", playground: "площадка",
  interactive: "интерактив", museum: "музей", culture: "культура", park: "парк", walk: "прогулка", view: "вид", unusual: "необычное",
  night: "ночью", nightlife: "бар/клуб", free: "бесплатно", indoor: "под крышей", accessible: "без ступенек", romantic: "свидание",
  photo: "фото", shopping: "шопинг", event: "события", cheap: "недорого", dog: "с собакой" };

async function loadDemand() {
  let d;
  try { d = await api.cityDemand(); } catch (_) { return; }
  $("#k").textContent = d.k;
  const max = Math.max(1, ...d.needs.map((x) => x.asked));
  $("#needs").innerHTML = d.needs.length ? d.needs.slice(0, 8).map((x) => `<div class="bar"><span>${esc(NEED[x.need] || x.label)}</span>
      <span class="track" title="не нашли ${x.unmet} из ${x.asked}"><span class="fill bad" style="display:block;width:${(100 * x.unmet) / max}%"></span></span>
      <em>${x.unmet} из ${x.asked}</em></div>`).join("")
    : `<span class="empty">Пока мало запросов: ни одна ячейка не набрала ${d.k} одинаковых просьб без ответа.</span>`;
  $("#live").innerHTML = d.live.length ? d.live.slice(0, 8).map((x) => `<div class="gap" style="cursor:default"><b>${esc(NEED[x.need] || x.need)}</b><span>ячейка ${esc(x.cell)} · ${x.n} ${plural(x.n, ["раз", "раза", "раз"])} не нашли</span></div>`).join("")
    : `<span class="empty">За последние 3 часа все просьбы нашли ответ.</span>`;
}

// ---------------------------------------------------------------- the data
async function loadData() {
  let d;
  try { d = await api.cityData(); } catch (_) { return; }
  const p = d.places, e = d.events;
  const kpis = [
    [n0(p.n), `${plural(p.n, ["место", "места", "мест"])} из OpenStreetMap${p.dgis_new ? " и 2ГИС" : ""}`],
    [n0(e.n), `${plural(e.n, ["событие", "события", "событий"])} из ${d.sources.filter((s) => s.kind !== "osm" && s.items).length} афиш и сайтов`],
    [`${pct(p.with_hours, p.n)}%`, "мест с часами работы — остальным пишем «часы неизвестны»", pct(p.with_hours, p.n) < 50],
    [`${pct(p.with_wheelchair, p.n)}%`, "мест с отметкой о доступности для колясок", pct(p.with_wheelchair, p.n) < 30],
    ...(p.with_rating ? [[n0(p.with_rating), `${plural(p.with_rating, ["место", "места", "мест"])} с рейтингом и отзывами 2ГИС`]] : []),
    [n0(d.tours.facts), `${plural(d.tours.facts, ["факт", "факта", "фактов"])} в ${d.tours.n} прогулках, у каждого — ссылка на источник`],
  ];
  $("#kpis").innerHTML = kpis.map(([b, s, warn]) => `<div class="kpi${warn ? " warn" : ""}"><b>${esc(b)}</b><span>${esc(s)}</span></div>`).join("");
  const bars = [
    ["часы работы", p.with_hours, p.n], ["доступность", p.with_wheelchair, p.n], ["телефон или сайт", p.with_contacts, p.n],
    ...(p.with_rating ? [["рейтинг 2ГИС", p.with_rating, p.n]] : []),
    ["событие на карте", e.with_place + e.geocoded, e.n], ["дата подтверждена цитатой", e.evidence_ok, e.n],
  ];
  $("#coverage").innerHTML = bars.map(([l, a, b]) => `<div class="bar"><span>${esc(l)}</span><span class="track"><span class="fill${pct(a, b) < 30 ? " bad" : ""}" style="display:block;width:${pct(a, b)}%"></span></span><em>${pct(a, b)}%</em></div>`).join("")
    + `<p class="why">Вручную уточнено ${d.curated.places} ${plural(d.curated.places, ["место", "места", "мест"])}: как их называют люди («Чупа-чупс», «Пирамида»), часы ${d.curated.with_hours} ключевых мест — с официальных сайтов.${p.dgis_enriched + p.dgis_new ? ` Из 2ГИС: часы ${p.hours_2gis} мест, доступность ${p.wheelchair_2gis}, Wi‑Fi ${p.wifi_2gis}, ${p.dgis_new} новых мест, которых нет в OSM.` : ""} Событий: ${e.jsonld} из разметки schema.org, ${e.llm} прочитал Claude, ${e.telegram} из Telegram.</p>`;
  $("#schedule").innerHTML = `<ul class="plain">${d.schedule.map((s) => `<li><b>${esc(s.what)}</b> — ${esc(s.when)}</li>`).join("")}<li><b>Погода и воздух</b> — ${esc(d.weather)}</li></ul>
    <p class="muted" style="margin:10px 0 0">База обновлена: места ${esc(ago(p.fetched_at))}, события ${esc(ago(e.last_seen_at))}.</p>`;
  $("#sources").innerHTML = `<table><thead><tr><th>Источник</th><th>Тип</th><th style="text-align:right">В базе</th><th>Последний сбор</th></tr></thead><tbody>${
    d.sources.map((s) => `<tr><td>${s.url ? `<a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title)}</a>` : esc(s.title)}</td>
      <td class="muted">${esc(SRC_KIND[s.kind] || s.kind)}</td>
      <td class="cnt${s.items ? "" : " zero"}">${n0(s.items)} <span class="muted">${esc(s.unit)}</span></td>
      <td class="muted">${esc(ago(s.last_run_at))}${s.status && s.status.startsWith("error") ? " · ошибка" : ""}</td></tr>`).join("")}</tbody></table>`;
}

// ---------------------------------------------------------------- start
async function start() {
  $("#logo").innerHTML = LOGO;
  let cfg = { map_style: { dark: "" } };
  try { cfg = await api.config(); } catch (_) { /* offline */ }
  map = new maplibregl.Map({ container: "cmap", style: cfg.map_style.dark || { version: 8, sources: {}, layers: [] }, center: ASTANA, zoom: 11.2,
                             attributionControl: false, cooperativeGestures: true });
  map.addControl(new maplibregl.AttributionControl({ compact: true, customAttribution: "© OpenStreetMap contributors" }));
  // no basemap (blocked CDN, venue Wi-Fi): the cells on a plain background instead of an empty map
  let styled = false;
  const plain = () => { if (!styled) { styled = true; map.setStyle({ version: 8, sources: {}, layers: [{ id: "bg", type: "background", paint: { "background-color": "#1B1612" } }] }); } };
  const fallback = setTimeout(plain, 3500);
  map.on("error", () => { if (!styled) plain(); });
  map.on("style.load", () => {
    styled = true;
    clearTimeout(fallback);
    if (!map.getSource("gaps")) {
      map.addSource("gaps", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      map.addLayer({ id: "gaps-fill", type: "fill", source: "gaps", paint: { "fill-color": ["get", "color"], "fill-opacity": ["get", "op"] } });
      map.addLayer({ id: "gaps-line", type: "line", source: "gaps", paint: { "line-color": ["get", "color"], "line-opacity": 0.8, "line-width": 1 } });
    }
    drawCells();
  });
  map.on("click", "gaps-fill", (ev) => focusCell(ev.features[0].properties.cell));
  map.on("mouseenter", "gaps-fill", () => { map.getCanvas().style.cursor = "pointer"; });
  map.on("mouseleave", "gaps-fill", () => { map.getCanvas().style.cursor = ""; });

  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-s]");
    if (b) loadGaps(b.dataset.s);
    const g = e.target.closest("[data-cell]");
    if (g) focusCell(g.dataset.cell);
  });
  document.addEventListener("keydown", (e) => {
    const g = e.target.closest && e.target.closest("[data-cell]");
    if (g && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); focusCell(g.dataset.cell); }
  });

  try { scenarios = await api.cityScenarios(); } catch (_) { scenarios = []; }
  const wanted = new URLSearchParams(location.search).get("s");
  await Promise.all([loadGaps(scenarios.some((s) => s.id === wanted) ? wanted : (scenarios[0] || {}).id), loadDemand(), loadData()]);
  setInterval(loadDemand, 15000);
}

start();
