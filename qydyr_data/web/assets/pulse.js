// "Пульс Астаны": anonymous live requests on the city map, for the projector during the pitch and for a phone.
// Every row says what was asked and whether Qydyr found it, in words, not tags.
import { api } from "./api.js";
import { I, LOGO } from "./icons.js";
import { ASTANA, hexagon } from "./map.js";
import * as maplibregl from "./vendor/maplibre-gl.mjs";

const NEED = {
  coffee: "кофе", food: "поесть", socket: "розетку", wifi: "Wi‑Fi", quiet: "тишину", kids: "с ребёнком", playground: "площадку",
  interactive: "интерактив", museum: "музей", culture: "культуру", park: "парк", walk: "прогулку", view: "красивый вид", unusual: "необычное",
  night: "ночью", nightlife: "бар или клуб", free: "бесплатно", indoor: "под крышей", accessible: "без ступенек", romantic: "свидание",
  photo: "место для фото", shopping: "шопинг", event: "событие", cheap: "недорого", dog: "с собакой",
};
// the bars read as a list of wishes: "кофе", "прогулка"
const NEED_SHORT = { ...NEED, socket: "розетка", quiet: "тишина", playground: "площадка", culture: "культура", walk: "прогулка", photo: "фото" };
const NEED_ICON = { coffee: "coffee", food: "coffee", kids: "kid", playground: "kid", accessible: "wheel", night: "moon", nightlife: "music",
  event: "event", museum: "flag", culture: "flag", walk: "walk", park: "walk", view: "star", photo: "camera", indoor: "home" };
const LANG = { kk: "на казахском", en: "на английском" };
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const ago = (s) => (s < 60 ? "только что" : s < 3600 ? `${Math.floor(s / 60)} мин назад` : `${Math.floor(s / 3600)} ч назад`);
// 1 место, 2 места, 5 мест; 21 место, 24 запроса
const plural = (n, [one, few, many]) => {
  const d = n % 10, h = n % 100;
  return d === 1 && h !== 11 ? one : d >= 2 && d <= 4 && (h < 12 || h > 14) ? few : many;
};
const COUNTERS = {
  places: [["место", "места", "мест"], " в базе"],
  events: [["событие", "события", "событий"], " сегодня"],
  req: [["запрос", "запроса", "запросов"], " за 3 часа"],
};

let map, seen = new Set(), first = true, lastFeed = "";

function setCounter(key, n) {
  const [forms, tail] = COUNTERS[key];
  $(`#c-${key}`).textContent = n == null ? "—" : n.toLocaleString("ru-RU");
  if (n != null) $(`#l-${key}`).textContent = plural(n, forms) + tail;
}

// Only whole rows: drop the ones the panel cannot show instead of cutting the last one in half (big screen only).
function fitRows(box) {
  if (getComputedStyle(box).overflow !== "hidden") return;
  while (box.children.length > 1 && box.scrollHeight > box.clientHeight + 1) box.lastElementChild.remove();
}

function askRow(r) {
  const words = r.needs.slice(0, 3).map((n) => NEED[n] || n);
  const what = r.surprise ? "Удиви меня" : words.length ? `Ищут ${words.join(", ")}` : "Что интересного рядом";
  const ic = r.surprise ? "dice" : NEED_ICON[r.needs[0]] || "search";
  const found = r.unmet && r.unmet.length ? `не нашлось: ${r.unmet.map((n) => NEED[n] || n).join(", ")}` : r.items ? `нашлось ${r.items} ${plural(r.items, ["вариант", "варианта", "вариантов"])}` : "";
  const sub = [found, LANG[r.lang]].filter(Boolean).join(" · ");
  return `<div class="ask"><span class="av${r.surprise ? " sun" : ""}">${I[ic] ? I[ic]("i") : ""}</span>
    <span class="txt">${esc(what)}${sub ? `<small>${esc(sub)}</small>` : ""}</span><span class="ago">${ago(r.ago_s)}</span></div>`;
}

async function start() {
  $("#logo").innerHTML = LOGO;
  let cfg = { map_style: { light: "" }, bot_username: "" };
  try { cfg = await api.config(); } catch (_) { /* offline */ }
  const link = cfg.bot_username ? `https://t.me/${cfg.bot_username}` : `${location.origin}/app/`;
  $("#qr").src = `/qr.svg?data=${encodeURIComponent(link)}`;
  $("#qr-url").textContent = link.replace(/^https?:\/\//, "");
  map = new maplibregl.Map({ container: "pmap", style: cfg.map_style.light || { version: 8, sources: {}, layers: [] }, center: ASTANA, zoom: 11.4,
                             attributionControl: false, interactive: false });
  map.addControl(new maplibregl.AttributionControl({ compact: true, customAttribution: "© OpenStreetMap contributors" }));
  // no basemap (blocked CDN, venue Wi-Fi): the cells on a plain background instead of an empty map
  let styled = false;
  const plain = () => { if (!styled) { styled = true; map.setStyle({ version: 8, sources: {}, layers: [{ id: "bg", type: "background", paint: { "background-color": "#EDE3D2" } }] }); } };
  const fallback = setTimeout(plain, 4000);
  map.on("error", plain);
  map.on("style.load", () => {
    styled = true;
    clearTimeout(fallback);
    if (!map.getSource("cells")) {
      map.addSource("cells", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      map.addLayer({ id: "cells-fill", type: "fill", source: "cells", paint: { "fill-color": "#00788E", "fill-opacity": ["get", "op"] } });
      map.addLayer({ id: "cells-line", type: "line", source: "cells", paint: { "line-color": "#00788E", "line-opacity": ["min", 1, ["+", ["get", "op"], 0.2]], "line-width": 1.2 } });
    }
    tick();
  });
  tick();
  setInterval(tick, 4000);
  // the server's clock: in demo mode it is the frozen "now" the data is answered for
  const skew = cfg.now ? new Date(cfg.now).getTime() - Date.now() : 0;
  const clock = () => { $("#clock").textContent = `в эфире · ${new Date(Date.now() + skew).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", timeZone: "Asia/Almaty" })}`; };
  clock();
  setInterval(clock, 1000);
}

async function tick() {
  let p;
  try { p = await api.pulse(); } catch (_) { return; }
  setCounter("places", p.counters ? p.counters.places : null);
  setCounter("events", p.counters ? p.counters.events_today : null);
  setCounter("req", p.requests);
  $("#map-empty").hidden = p.cells.length > 0;
  const max = Math.max(1, ...p.cells.map((c) => c.n));
  const features = p.cells.map((c) => ({ type: "Feature", properties: { op: 0.18 + 0.7 * (c.n / max) }, geometry: { type: "Polygon", coordinates: [hexagon(c.lon, c.lat, 600)] } }));
  if (map && map.getSource("cells")) map.getSource("cells").setData({ type: "FeatureCollection", features });
  // new requests flash on the map
  for (const c of p.cells) {
    const key = `${c.cell}:${c.n}`;
    if (seen.has(key)) continue;
    seen.add(key);
    if (!first && c.last_s < 8) {
      // the marker element is positioned with a transform: the flash animates a child, not the marker itself
      const el = document.createElement("div");
      el.innerHTML = '<div class="flash"></div>';
      const mk = new maplibregl.Marker({ element: el }).setLngLat([c.lon, c.lat]).addTo(map);
      setTimeout(() => mk.remove(), 2500);
    }
  }
  first = false;
  const feed = p.recent.length ? p.recent.slice(0, 8).map(askRow).join("")
    : `<div class="quiet"><img src="assets/art/suyrbek-wave.webp" alt="" width="96" height="124">Пока тихо. Спросите первым — QR-код ниже.</div>`;
  if (feed !== lastFeed) {   // re-render (and re-animate) only when something changed
    lastFeed = feed;
    $("#feed").innerHTML = feed;
  }
  requestAnimationFrame(() => fitRows($("#feed")));   // after the counters and bars above settled
  const tmax = Math.max(1, ...p.top_needs.map((x) => x.n));
  $("#bars").innerHTML = p.top_needs.length ? p.top_needs.slice(0, 5).map((x) => `<div class="brow"><span>${esc(NEED_SHORT[x.need] || x.need)}</span><span class="track"><span class="fill" style="width:${(x.n / tmax) * 100}%"></span></span><span class="n">${x.n}</span></div>`).join("")
    : '<span class="empty">Появится после первых запросов.</span>';
  const items = (p.now_on || []).map((e) => `<span><b>${esc(e.title.replace(/^.*«|».*$/g, ""))}</b> <em>· ${esc(e.venue || "")}${e.venue ? " · " : ""}${esc(e.badge.split(" · ").filter((x) => !/^Идёт сейчас$/.test(x)).slice(0, 1).join(""))}</em></span>`);
  if (items.length) $("#ticker").innerHTML = items.join("") + items.join("");   // doubled for a seamless loop
  else $("#ticker").innerHTML = "<span><em>Сейчас в афише пусто</em></span>";
}

start();
