// "Пульс Астаны": anonymous live requests on the city map, for the projector during the pitch.
import { api } from "./api.js";
import { LOGO } from "./icons.js";
import { ASTANA, hexagon } from "./map.js";
import * as maplibregl from "./vendor/maplibre-gl.mjs";

const NEED = {
  coffee: "кофе", food: "поесть", socket: "розетка", wifi: "Wi‑Fi", quiet: "тихо", kids: "с ребёнком", playground: "площадка",
  interactive: "интерактив", museum: "музей", culture: "культура", park: "парк", walk: "прогулка", view: "вид", unusual: "необычное",
  night: "ночью", nightlife: "бар/клуб", free: "бесплатно", indoor: "в помещении", accessible: "без ступенек", romantic: "свидание",
  photo: "фото", shopping: "шопинг", event: "события",
};
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const ago = (s) => (s < 60 ? `${s} с` : s < 3600 ? `${Math.floor(s / 60)} мин` : `${Math.floor(s / 3600)} ч`);
// 1 место, 2 места, 5 мест; 21 место, 24 запроса
const plural = (n, [one, few, many]) => {
  const d = n % 10, h = n % 100;
  return d === 1 && h !== 11 ? one : d >= 2 && d <= 4 && (h < 12 || h > 14) ? few : many;
};
const COUNTERS = [
  ["places", ["место", "места", "мест"], " в базе"],
  ["events", ["событие", "события", "событий"], " сегодня"],
  ["req", ["запрос", "запроса", "запросов"], " за 3 часа"],
];

let map, seen = new Set(), first = true, lastFeed = "";

function setCounter(key, n) {
  const [, forms, tail] = COUNTERS.find((c) => c[0] === key);
  $(`#c-${key}`).textContent = n == null ? "—" : n.toLocaleString("ru-RU");
  if (n != null) $(`#l-${key}`).textContent = plural(n, forms) + tail;
}

// Only whole rows: drop the ones the panel cannot show instead of cutting the last one in half.
function fitRows(box) {
  while (box.children.length > 1 && box.scrollHeight > box.clientHeight + 1) box.lastElementChild.remove();
}

async function start() {
  $("#logo").innerHTML = LOGO;
  let cfg = { map_style: { dark: "" }, bot_username: "" };
  try { cfg = await api.config(); } catch (_) { /* offline */ }
  const link = cfg.bot_username ? `https://t.me/${cfg.bot_username}` : `${location.origin}/app/`;
  $("#qr").src = `/qr.svg?data=${encodeURIComponent(link)}`;
  $("#qr-url").textContent = link.replace(/^https?:\/\//, "");
  map = new maplibregl.Map({ container: "pmap", style: cfg.map_style.dark || { version: 8, sources: {}, layers: [] }, center: ASTANA, zoom: 11.6, attributionControl: false, interactive: false });
  map.addControl(new maplibregl.AttributionControl({ compact: true, customAttribution: "© OpenStreetMap contributors" }));
  const fallback = setTimeout(() => { if (!map.isStyleLoaded()) map.setStyle({ version: 8, sources: {}, layers: [{ id: "bg", type: "background", paint: { "background-color": "#0A1424" } }] }); }, 6000);
  map.on("style.load", () => {
    clearTimeout(fallback);
    if (!map.getSource("cells")) {
      map.addSource("cells", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      map.addLayer({ id: "cells-fill", type: "fill", source: "cells", paint: { "fill-color": "#00AFCA", "fill-opacity": ["get", "op"] } });
      map.addLayer({ id: "cells-line", type: "line", source: "cells", paint: { "line-color": "#6FD8E8", "line-opacity": ["min", 1, ["+", ["get", "op"], 0.2]], "line-width": 1 } });
    }
    tick();
  });
  setInterval(tick, 4000);
  setInterval(() => { $("#clock").textContent = `в эфире · ${new Date().toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", timeZone: "Asia/Almaty" })}`; }, 1000);
}

async function tick() {
  let p;
  try { p = await api.pulse(); } catch (_) { return; }
  setCounter("places", p.counters ? p.counters.places : null);
  setCounter("events", p.counters ? p.counters.events_today : null);
  setCounter("req", p.requests);
  const max = Math.max(1, ...p.cells.map((c) => c.n));
  const features = p.cells.map((c) => ({ type: "Feature", properties: { op: 0.18 + 0.72 * (c.n / max) }, geometry: { type: "Polygon", coordinates: [hexagon(c.lon, c.lat, 600)] } }));
  if (map.getSource("cells")) map.getSource("cells").setData({ type: "FeatureCollection", features });
  // new requests flash on the map
  for (const c of p.cells) {
    const key = `${c.cell}:${c.n}`;
    if (!seen.has(key)) {
      seen.add(key);
      if (!first && c.last_s < 8) {
        const el = document.createElement("div");
        el.className = "bubble-cell";
        const mk = new maplibregl.Marker({ element: el }).setLngLat([c.lon, c.lat]).addTo(map);
        setTimeout(() => mk.remove(), 2500);
      }
    }
  }
  first = false;
  const feed = p.recent.length ? p.recent.slice(0, 7).map((r) => `<div class="feed-row"><span class="chips2">${r.surprise ? '<span class="tagn sun">удиви меня</span>' : ""}${r.needs.slice(0, 3).map((n) => `<span class="tagn">${esc(NEED[n] || n)}</span>`).join("")}${r.lang !== "ru" ? `<span class="tagn lang">${esc(r.lang.toUpperCase())}</span>` : ""}</span><span class="ago">${ago(r.ago_s)}</span></div>`).join("")
    : '<span class="empty">Пока тихо. Спросите первым — QR ниже.</span>';
  if (feed !== lastFeed) {   // re-render (and re-animate) only when something changed
    lastFeed = feed;
    $("#feed").innerHTML = feed;
    fitRows($("#feed"));
  }
  const tmax = Math.max(1, ...p.top_needs.map((x) => x.n));
  $("#bars").innerHTML = p.top_needs.length ? p.top_needs.slice(0, 5).map((x) => `<div class="brow"><span>${esc(NEED[x.need] || x.need)}</span><span><span class="bar" style="display:block;width:${(x.n / tmax) * 100}%"></span></span><span class="n">${x.n}</span></div>`).join("") : '<span class="empty">—</span>';
  const items = (p.now_on || []).map((e) => `<span>«${esc(e.title.replace(/^.*«|».*$/g, ""))}» · ${esc(e.badge.split(" · ").slice(0, 2).join(" · "))}</span>`);
  if (items.length) $("#ticker").innerHTML = items.join("") + items.join("");   // doubled for a seamless loop
}

start();
