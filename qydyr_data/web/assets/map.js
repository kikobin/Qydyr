// The city map: reach circle, ~150 m "you are here" cell, glowing picks, grey "why not" pins and a comet route.
import * as maplibregl from "./vendor/maplibre-gl.mjs";
import { mascot } from "./mascot.js";
import { curved, tourLine } from "./walk.js";

export const ASTANA = [71.4304, 51.1282];

export function circle(lon, lat, radiusM, steps = 72) {
  const pts = [];
  const dLat = radiusM / 111320;
  const dLon = radiusM / (111320 * Math.cos((lat * Math.PI) / 180));
  for (let i = 0; i <= steps; i++) {
    const a = (i / steps) * 2 * Math.PI;
    pts.push([lon + dLon * Math.cos(a), lat + dLat * Math.sin(a)]);
  }
  return { type: "Feature", geometry: { type: "Polygon", coordinates: [pts] }, properties: {} };
}

export function hexagon(lon, lat, radiusM) {
  const pts = [];
  const dLat = radiusM / 111320;
  const dLon = radiusM / (111320 * Math.cos((lat * Math.PI) / 180));
  for (let i = 0; i <= 6; i++) {
    const a = (i / 6) * 2 * Math.PI;
    pts.push([lon + dLon * Math.cos(a), lat + dLat * Math.sin(a)]);
  }
  return pts;
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

const FALLBACK = (bg) => ({ version: 8, sources: {}, layers: [{ id: "bg", type: "background", paint: { "background-color": bg } }] });

// Long names are cut at a word boundary: "Национальный музей Республики Казахстан" -> "Национальный музей…".
export function shortLabel(title, max = 24) {
  if (title.length <= max + 2) return title;
  const cut = title.slice(0, max);
  const sp = cut.lastIndexOf(" ");
  let out = sp > 8 ? cut.slice(0, sp) : cut;
  const open = out.lastIndexOf("«");
  if (open > 8 && out.indexOf("»", open) < 0) out = out.slice(0, open);   // never leave a quote unclosed
  return `${out.replace(/[\s,«(–-]+$/, "")}…`;
}

export class CityMap {
  constructor(container, { styleUrl, bg, attribution, onPick, onReject }) {
    this.container = container;
    this.styleUrl = styleUrl;
    this.bg = bg;
    this.onPick = onPick;
    this.onReject = onReject;
    this.markers = [];
    this.data = null;
    this.anim = null;
    this.ready = new Promise((resolve) => (this._ready = resolve));
    this.map = new maplibregl.Map({
      container, style: styleUrl, center: ASTANA, zoom: 13, attributionControl: false, pitchWithRotate: false,
      dragRotate: false, fadeDuration: 0,
    });
    this.map.addControl(new maplibregl.AttributionControl({ compact: true, customAttribution: attribution }), "bottom-right");
    this.map.touchZoomRotate.disableRotation();
    this._styled = false;   // style.load has fired: sources and layers can be added (tiles may still be loading)
    const fallback = () => {
      if (this._fellBack) return;
      this._fellBack = true;
      this._styled = false;
      console.warn("map style unavailable, using offline background");
      this.map.setStyle(FALLBACK(this.bg));
    };
    // no basemap (blocked CDN, offline Wi-Fi): switch to the plain background instead of an empty map
    const timer = setTimeout(() => { if (!this._styled) fallback(); }, 5000);
    this.map.on("error", () => { if (!this._styled) fallback(); });   // any error before the style arrives
    this.map.on("moveend", () => this._declutter());
    this.map.on("style.load", () => {
      this._styled = true;
      clearTimeout(timer);
      this._layers();
      if (this.mode === "tour" && this.tourData) this.showTour(this.tourData.tour, { ...this.tourData, fit: !!this._fitPending });
      else if (this.data) this.show(this.data, this.sel);
      this._ready();
    });
  }

  setStyle(styleUrl, bg) {
    this.styleUrl = styleUrl;
    this.bg = bg;
    this._fellBack = false;
    this._styled = false;
    this.map.setStyle(styleUrl);
  }

  _layers() {
    const m = this.map;
    const css = getComputedStyle(document.documentElement);
    const pin = css.getPropertyValue("--pin").trim() || "#A8361F";     // routes and "you can reach this far"
    const you = css.getPropertyValue("--you").trim() || "#0A7BA2";     // where you are
    const empty = { type: "FeatureCollection", features: [] };
    for (const id of ["reach", "me-cell", "route"]) {
      if (!m.getSource(id)) m.addSource(id, { type: "geojson", data: empty, lineMetrics: id === "route" });
    }
    if (!m.getLayer("reach-fill")) {
      m.addLayer({ id: "reach-fill", type: "fill", source: "reach", paint: { "fill-color": pin, "fill-opacity": 0.05 } });
      m.addLayer({ id: "reach-line", type: "line", source: "reach", paint: { "line-color": pin, "line-opacity": 0.6, "line-width": 1.6, "line-dasharray": [3, 3] } });
      m.addLayer({ id: "me-fill", type: "fill", source: "me-cell", paint: { "fill-color": you, "fill-opacity": 0.14 } });
      m.addLayer({ id: "me-line", type: "line", source: "me-cell", paint: { "line-color": you, "line-opacity": 0.8, "line-width": 1.2, "line-dasharray": [2, 2] } });
      m.addLayer({ id: "route-glow", type: "line", source: "route", layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": "#FFFFFF", "line-width": 8, "line-opacity": 0.9 } });
      m.addLayer({ id: "route-line", type: "line", source: "route", layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-width": 4, "line-gradient": ["interpolate", ["linear"], ["line-progress"], 0, pin, 1, pin] } });
    }
  }

  clear() {
    this.markers.forEach((mk) => mk.remove());
    this.markers = [];
    cancelAnimationFrame(this.anim);
    this.anim = null;
    const empty = { type: "FeatureCollection", features: [] };
    for (const id of ["reach", "me-cell", "route"]) this.map.getSource(id) && this.map.getSource(id).setData(empty);
  }

  show(answer, sel = 0, { labels = {}, fit = true } = {}) {
    this.mode = "answer";
    this.data = answer;
    this.sel = sel;
    if (!this._styled) return;   // drawn once the style loads
    this.clear();
    const a = answer.anchor;
    if (!a) return;
    const ctx = answer.context || {};
    if (ctx.radius_m) this.map.getSource("reach").setData(circle(a.lon, a.lat, ctx.radius_m));
    if (a.source === "gps") this.map.getSource("me-cell").setData(circle(a.lon, a.lat, 150, 48));
    const me = el("div", "me");
    me.setAttribute("aria-label", labels.you || "вы здесь");
    this._add(me, a.lon, a.lat);

    (answer.rejected || []).forEach((r, i) => {
      const d = el("button", "pin-off");
      d.type = "button";
      d.setAttribute("aria-label", `${r.title}: ${r.reason}`);
      d.addEventListener("click", (ev) => { ev.stopPropagation(); this.onReject && this.onReject(i); this.popup(r); });
      this._add(d, r.lon, r.lat);
    });
    const names = (answer.items || []).map((it) => shortLabel(it.title));
    this._labelMax = Math.max(0, ...names.map((n) => n.length));
    (answer.items || []).forEach((it, i) => {
      const d = el("button", `pin${i === sel ? " sel" : ""}`);
      d.type = "button";
      d.setAttribute("aria-label", `${i + 1}. ${it.title}`);
      d.append(el("span", "ring"), el("span", "core", String(i + 1)), el("span", "pin-label", names[i]));
      d.addEventListener("click", (ev) => { ev.stopPropagation(); this.onPick && this.onPick(i); });
      this._add(d, it.lon, it.lat, i === sel ? 3 : 2);
    });

    const r = answer.route;
    if (r && r.geometry && r.geometry.length > 1) {
      const coords = r.geometry_source === "straight" ? curved(r.geometry) : r.geometry;
      this.map.getSource("route").setData({ type: "Feature", geometry: { type: "LineString", coordinates: coords }, properties: {} });
      this._comet(coords);
    }
    if (fit) this.fit(answer); else requestAnimationFrame(() => this._declutter());
  }

  // A pin label goes where it covers nothing: right of its pin, else left, below or above. A label with no free
  // place hides (the name is one tap away), except the next stop / the top pick, which takes the least crowded.
  _declutter() {
    const els = this.markers.map((mk) => mk.getElement());
    const pins = els.filter((e) => e.classList.contains("pin"));
    if (!pins.length) return;
    const PLACES = ["", "lab-left", "lab-below", "lab-above"];
    const box = (e, pad = 1) => { const r = e.getBoundingClientRect(); return [r.left - pad, r.top - pad, r.right + pad, r.bottom + pad]; };
    const area = (a, b) => Math.max(0, Math.min(a[2], b[2]) - Math.max(a[0], b[0])) * Math.max(0, Math.min(a[3], b[3]) - Math.max(a[1], b[1]));
    pins.forEach((e) => e.classList.remove("lab-hid", ...PLACES.filter(Boolean)));
    // pins are circles: the corners of their boxes are empty, so a label may touch them; the walker counts too,
    // the comet does not (it never stops moving)
    const obstacles = els.filter((e) => !e.classList.contains("comet")).map((e) => [e, box(e, -4)]);
    // the search bar, chips, weather badge and the sheet cover the map: a label under them is as good as hidden
    const cover = ["ask", "chips", "wx", "sheet"].map((id) => document.getElementById(id)).filter(Boolean).map((e) => box(e, 0));
    const W = this.container.clientWidth;
    const taken = [];
    const order = pins.map((_, i) => i).sort((a, b) => (pins[b].classList.contains("sel") - pins[a].classList.contains("sel")) || a - b);
    order.forEach((i, rank) => {
      const pin = pins[i];
      const lab = pin.querySelector(".pin-label");
      if (!lab) return;
      let best = null;
      for (const cls of PLACES) {
        if (cls) pin.classList.add(cls);
        const b = box(lab);
        const off = b[0] < 0 || b[2] > W || b[1] < 0 ? 1e6 : 0;
        const cost = off + obstacles.reduce((sum, [e, o]) => sum + (e === pin || (rank === 0 && e.classList.contains("me")) ? 0 : area(b, o)), 0)
          + taken.reduce((sum, t) => sum + area(b, t), 0) + cover.reduce((sum, r) => sum + 50 * area(b, r), 0);
        if (cls) pin.classList.remove(cls);
        if (!best || cost < best.cost) best = { cls, b, cost };
        if (cost === 0) break;
      }
      if (best.cost > 0 && rank > 0) { pin.classList.add("lab-hid"); return; }
      if (best.cls) pin.classList.add(best.cls);
      taken.push(best.b);
    });
  }

  _add(node, lon, lat, z = 1) {
    const mk = new maplibregl.Marker({ element: node, anchor: "center" }).setLngLat([lon, lat]).addTo(this.map);
    mk.getElement().style.zIndex = String(z);
    this.markers.push(mk);
    return mk;
  }

  popup(r) {
    if (this._popup) this._popup.remove();
    const box = el("div");
    box.append(el("div", "", r.title));
    const reason = el("div", "", r.reason);
    reason.style.cssText = "font-size:13px;color:var(--text-2);margin-top:2px;max-width:220px";
    box.append(reason);
    box.firstChild.style.cssText = "font-weight:600;font-size:14px";
    this._popup = new maplibregl.Popup({ closeButton: false, offset: 14, maxWidth: "260px" }).setLngLat([r.lon, r.lat]).setDOMContent(box).addTo(this.map);
  }

  _comet(coords) {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const seg = [0];
    for (let i = 1; i < coords.length; i++) {
      const dx = coords[i][0] - coords[i - 1][0], dy = coords[i][1] - coords[i - 1][1];
      seg.push(seg[i - 1] + Math.hypot(dx, dy * 1.6));
    }
    const total = seg[seg.length - 1] || 1;
    const head = this._add(el("div", "comet"), coords[0][0], coords[0][1], 4);
    const period = 2600, pause = 900;
    const t0 = performance.now();
    const step = (now) => {
      const t = ((now - t0) % (period + pause)) / period;
      const d = Math.min(t, 1) * total;
      let i = 1;
      while (i < seg.length - 1 && seg[i] < d) i++;
      const f = (d - seg[i - 1]) / ((seg[i] - seg[i - 1]) || 1);
      head.setLngLat([coords[i - 1][0] + (coords[i][0] - coords[i - 1][0]) * f, coords[i - 1][1] + (coords[i][1] - coords[i - 1][1]) * f]);
      head.getElement().style.opacity = t > 1 ? "0" : "1";
      this.anim = requestAnimationFrame(step);
    };
    this.anim = requestAnimationFrame(step);
  }

  // easeTo keeps a `padding` for every later move, and fitBounds adds its own on top of it (the map then
  // "cannot fit"), so single points are framed with a one-off offset to the middle of the visible area instead
  _offset() {
    const p = this.padding();
    return [(p.left - p.right) / 2, (p.top - p.bottom) / 2];
  }

  padding() {
    const desktop = window.innerWidth >= 900;
    // pin labels hang to the right of the pin, so the right edge keeps room for the longest one
    const label = Math.min(230, 44 + 7.5 * (this._labelMax || 0));
    if (desktop) return { top: 80, bottom: 60, left: 470, right: Math.max(80, label) };
    // the sheet covers the bottom of the map: keep every pin, with its 15 px radius, above its edge
    const H = window.innerHeight, top = 215;
    const sheet = document.getElementById("sheet");
    const covered = sheet ? H - sheet.getBoundingClientRect().top : H * 0.5;
    return { top, bottom: Math.round(Math.min(Math.max(covered, H * 0.3) + 32, H - top - 140)), left: 40, right: Math.max(48, label) };
  }

  fit(answer) {
    const pts = [[answer.anchor.lon, answer.anchor.lat], ...(answer.items || []).map((i) => [i.lon, i.lat])];
    if (pts.length === 1) { this.map.easeTo({ center: pts[0], zoom: 15, offset: this._offset(), duration: 700 }); return; }
    const b = new maplibregl.LngLatBounds(pts[0], pts[0]);
    pts.forEach((p) => b.extend(p));
    this.map.fitBounds(b, { padding: this.padding(), maxZoom: 16.2, duration: 800 });
  }

  focus(lon, lat) {
    this.map.easeTo({ center: [lon, lat], zoom: Math.max(this.map.getZoom(), 15), offset: this._offset(), duration: 600 });
  }

  center(lon, lat, zoom = 13.5) { this.map.jumpTo({ center: [lon, lat], zoom }); }

  // ---- walk mode: the tour's route, numbered stops (ticked when visited, pulsing when next), the walker
  showTour(tour, { visited = [], next = null, me = null, fit = true, onStop = null, mood = "hello", doneLabel = "done" } = {}) {
    this.mode = "tour";
    this.tourData = { tour, visited, next, me, onStop, mood, doneLabel };
    if (!this._styled) { this._fitPending = fit; return; }   // drawn (and fitted) once the style loads
    this._fitPending = false;
    this.clear();
    this.walker = null;
    this.map.getSource("route").setData({ type: "Feature", geometry: { type: "LineString", coordinates: tourLine(tour) }, properties: {} });
    const names = tour.stops_list.map((s) => shortLabel(s.name));
    // only stops still ahead carry a label, so only they need room on the right
    this._labelMax = Math.max(0, ...names.filter((_, i) => !visited.includes(tour.stops_list[i].id)).map((n) => n.length));
    tour.stops_list.forEach((s, i) => {
      const done = visited.includes(s.id);
      const d = el("button", `pin stop${done ? " done" : ""}${s.id === next ? " sel next" : ""}`);
      d.type = "button";
      d.setAttribute("aria-label", done ? `${s.n}. ${s.name}, ${doneLabel}` : `${s.n}. ${s.name}`);
      const core = el("span", "core", done ? null : String(s.n));
      if (done) core.innerHTML = CHECK;
      d.append(el("span", "ring"), core);
      if (!done) d.append(el("span", "pin-label", names[i]));   // visited stops fold into a tick: less clutter
      d.addEventListener("click", (ev) => { ev.stopPropagation(); onStop && onStop(i); });
      this._add(d, s.lon, s.lat, s.id === next ? 3 : 2);
    });
    if (me) this.setWalker(me.lon, me.lat, { mood });
    if (fit) this.fitTour(tour, me); else requestAnimationFrame(() => this._declutter());
  }

  setWalker(lon, lat, { follow = false, mood } = {}) {
    if (this.tourData) this.tourData.me = { lon, lat };
    if (!this.walker || !this.markers.includes(this.walker)) {
      const w = el("div", "walker");
      w.innerHTML = mascot(mood || (this.tourData && this.tourData.mood) || "hello", { size: 46 }) + '<span class="foot"></span>';
      this.walker = new maplibregl.Marker({ element: w, anchor: "bottom" }).setLngLat([lon, lat]).addTo(this.map);
      this.walker.getElement().style.zIndex = "6";
      this.markers.push(this.walker);
    } else this.walker.setLngLat([lon, lat]);
    const now = performance.now();   // labels step aside for the walker, at most twice a second
    if (!this._declAt || now - this._declAt > 500) { this._declAt = now; this._declutter(); }
    if (follow) {
      const b = this.map.getBounds();
      const pad = 0.18;
      const w = b.getEast() - b.getWest(), h = b.getNorth() - b.getSouth();
      const inside = lon > b.getWest() + w * pad && lon < b.getEast() - w * pad && lat > b.getSouth() + h * 0.45 && lat < b.getNorth() - h * pad;
      if (!inside) this.map.easeTo({ center: [lon, lat], offset: this._offset(), duration: 500 });
    }
  }

  fitTour(tour, me) {
    const pts = tour.stops_list.map((s) => [s.lon, s.lat]);
    if (me) pts.push([me.lon, me.lat]);
    const b = new maplibregl.LngLatBounds(pts[0], pts[0]);
    pts.forEach((p) => b.extend(p));
    this.map.fitBounds(b, { padding: this.padding(), maxZoom: 16.6, duration: 800 });
  }
}

const CHECK = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12.5l4.2 4.2L19 7"/></svg>';
