// Walk mode on the phone: the same arrival rules as qydyr_data/assistant/tours.py, run locally, so the exact
// position never leaves the device. Plus a demo walker that drives the dot along the route for the stage.

const R = 6371000;
const DETOUR = 1.3;          // street vs straight line
const WALK_MPM = 4.8 * 1000 / 60;
export const ARRIVE_SLACK_M = 40;
export const OFF_ROUTE_M = 300;

const rad = (x) => (x * Math.PI) / 180;

export function haversine(lat1, lon1, lat2, lon2) {
  const p1 = rad(lat1), p2 = rad(lat2), dp = p2 - p1, dl = rad(lon2 - lon1);
  const a = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(a));
}

function segDist(lat, lon, a, b) {
  const k = Math.cos(rad(lat));
  const px = lon * k, py = lat, ax = a[1] * k, ay = a[0], bx = b[1] * k, by = b[0];
  const dx = bx - ax, dy = by - ay;
  const t = dx === 0 && dy === 0 ? 0 : Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)));
  return haversine(lat, lon, ay + t * dy, (ax + t * dx) / k);
}

function routeDist(lat, lon, pts) {
  if (!pts.length) return Infinity;
  if (pts.length === 1) return haversine(lat, lon, pts[0][0], pts[0][1]);
  let best = Infinity;
  for (let i = 1; i < pts.length; i++) best = Math.min(best, segDist(lat, lon, pts[i - 1], pts[i]));
  return best;
}

export function walkMinutes(m) { return m <= 60 ? 0 : Math.max(1, Math.round(m / WALK_MPM)); }

export function nextStop(stops, visited) {
  const seen = new Set(visited);
  const idx = stops.map((s, i) => (seen.has(s.id) ? i : -1)).filter((i) => i >= 0);
  const start = idx.length ? Math.max(...idx) + 1 : 0;
  const order = [...stops.slice(start), ...stops.slice(0, start)];
  return order.find((s) => !seen.has(s.id)) || null;
}

// One location update -> what changed. Mirrors tours.progress().
export function progress(tour, lat, lon, visited = [], accuracy = 0) {
  const stops = tour.stops_list;
  const seen = [...new Set(visited)].filter((id) => stops.some((s) => s.id === id));
  const slack = ARRIVE_SLACK_M + Math.min(Math.max(accuracy || 0, 0), 60);
  let arrived = null, best = Infinity;
  for (const s of stops) {
    if (seen.includes(s.id)) continue;
    const d = haversine(lat, lon, s.lat, s.lon);
    if (d <= s.radius_m + slack && d < best) { best = d; arrived = s; }
  }
  if (arrived) seen.push(arrived.id);
  const nxt = nextStop(stops, seen);
  const out = { arrived: arrived ? arrived.id : null, visited: seen, done: !nxt, next: nxt ? nxt.id : null, next_m: null, next_min: null, off_route: false };
  if (nxt) {
    const m = haversine(lat, lon, nxt.lat, nxt.lon) * DETOUR;
    out.next_m = Math.round(m);
    out.next_min = walkMinutes(m);
    if (seen.length) {
      const idx = (id) => stops.findIndex((s) => s.id === id);
      const last = stops[Math.max(...seen.map(idx))];
      let rest = stops.slice(idx(nxt.id)).map((s) => [s.lat, s.lon]);
      if (idx(last.id) < idx(nxt.id)) rest = [[last.lat, last.lon], ...rest];
      out.off_route = routeDist(lat, lon, rest) > OFF_ROUTE_M;
    }
  }
  return out;
}

// A gentle curve between stops (the map draws the same one): the straight line is an honest sketch,
// the curve reads as a route.
export function curved(points) {
  if (points.length < 2) return points;
  const out = [points[0]];
  for (let i = 1; i < points.length; i++) {
    const [x0, y0] = points[i - 1];
    const [x1, y1] = points[i];
    const mx = (x0 + x1) / 2, my = (y0 + y1) / 2;
    const k = (i % 2 ? 0.14 : -0.14);
    const cx = mx - (y1 - y0) * k * 1.6, cy = my + (x1 - x0) * k * 0.62;
    for (let t = 1 / 16; t <= 1.0001; t += 1 / 16) {
      const a = (1 - t) * (1 - t), b = 2 * (1 - t) * t, c = t * t;
      out.push([a * x0 + b * cx + c * x1, a * y0 + b * cy + c * y1]);
    }
  }
  return out;
}

export function tourLine(tour) {
  const pts = tour.stops_list.map((s) => [s.lon, s.lat]);
  return tour.geometry_source === "osrm" && tour.geometry && tour.geometry.length > 1 ? tour.geometry : curved(pts);
}

// The demo walker: a point every ~6 m along the drawn route, starting a little before the first stop, so
// the dot glides and the stops trigger by the same rules a real walk uses.
export function demoPath(tour) {
  const stops = tour.stops_list;
  const s0 = stops[0], s1 = stops[1] || stops[0];
  const lead = [s0.lon - (s1.lon - s0.lon) * 0.2, s0.lat - (s1.lat - s0.lat) * 0.2];
  return resample([lead, ...tourLine(tour)], 6).map(([lon, lat]) => ({ lat, lon }));
}

function resample(pts, stepM) {
  const out = [pts[0]];
  for (let i = 1; i < pts.length; i++) {
    const [x0, y0] = pts[i - 1], [x1, y1] = pts[i];
    const d = haversine(y0, x0, y1, x1);
    const n = Math.max(1, Math.round(d / stepM));
    for (let k = 1; k <= n; k++) out.push([x0 + (x1 - x0) * (k / n), y0 + (y1 - y0) * (k / n)]);
  }
  return out;
}

export function pathLength(points) {
  let m = 0;
  for (let i = 1; i < points.length; i++) m += haversine(points[i - 1].lat, points[i - 1].lon, points[i].lat, points[i].lon);
  return m;
}
