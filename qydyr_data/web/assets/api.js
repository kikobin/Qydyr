// API client. Inside Telegram every request carries initData, which the server verifies.
import { initData } from "./tg.js";

const BASE = (window.QYDYR_API || "").replace(/\/$/, "");

async function call(method, path, body) {
  const headers = { "Content-Type": "application/json" };
  const init = initData();
  if (init) headers["X-Telegram-Init-Data"] = init;
  const r = await fetch(BASE + path, { method, headers, body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) {
    const err = new Error(`${r.status} ${path}`);
    err.status = r.status;
    throw err;
  }
  return r.json();
}

export const api = {
  config: () => call("GET", "/config"),
  ask: (body) => call("POST", "/ask", { format: "structured", ...body }),
  answer: (id) => call("GET", `/answer/${encodeURIComponent(id)}`),
  walks: (q) => call("GET", `/walks?${new URLSearchParams(q)}`),
  place: (id, at) => call("GET", `/place/${encodeURI(id)}${at ? `?at=${encodeURIComponent(at)}` : ""}`),
  placeAsk: (id, q, at) => call("POST", `/place/${encodeURI(id)}/ask`, { q, at }),
  story: (id, lang) => call("GET", `/story/${encodeURI(id)}?lang=${lang}`),
  feedback: (answer_id, vote, item) => call("POST", "/feedback", { answer_id, vote, item }),
  pulse: () => call("GET", "/pulse"),
  tours: (lang) => call("GET", `/tours?lang=${lang}`),
  tour: (id, lang) => call("GET", `/tours/${encodeURIComponent(id)}?lang=${lang}`),
  tourStory: (id, stop, lang) => call("GET", `/tours/${encodeURIComponent(id)}/stops/${encodeURIComponent(stop)}/story?lang=${lang}`),
  nudges: (q) => call("GET", `/walk/nudges?${new URLSearchParams(q)}`),
  weather: (q) => call("GET", `/weather?${new URLSearchParams(q)}`),
  whereami: async (file, lang) => {
    const fd = new FormData();
    fd.append("photo", file);
    fd.append("lang", lang);
    const headers = {};
    const init = initData();
    if (init) headers["X-Telegram-Init-Data"] = init;
    const r = await fetch(BASE + "/whereami", { method: "POST", body: fd, headers });
    if (!r.ok) { const e = new Error(`${r.status} /whereami`); e.status = r.status; throw e; }
    return r.json();
  },
};
