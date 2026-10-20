// Telegram Mini App integration with graceful fallbacks: the same page works in a normal browser.
const W = window.Telegram && window.Telegram.WebApp;
export const inTelegram = Boolean(W && W.initData);
export const tg = W || null;

const atLeast = (v) => Boolean(W && W.isVersionAtLeast && W.isVersionAtLeast(v));

export function initTelegram({ onBack, theme }) {
  if (!W) return;
  try {
    W.ready();
    W.expand();
    if (atLeast("7.7")) W.disableVerticalSwipes();          // panning the map must not close the app
    const bg = theme === "light" ? "#EEF3F8" : "#0A1424";
    if (atLeast("6.1")) { W.setHeaderColor(bg); W.setBackgroundColor(bg); }
    if (atLeast("7.10")) W.setBottomBarColor(bg);
    W.BackButton.onClick(onBack);
  } catch (e) { console.warn("telegram init", e); }
}

export function telegramTheme() {
  if (W && W.colorScheme) return W.colorScheme;           // "dark" | "light"
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

export function backButton(visible) {
  if (!W || !atLeast("6.1")) return;
  visible ? W.BackButton.show() : W.BackButton.hide();
}

export const haptic = {
  tap() { try { atLeast("6.1") && W.HapticFeedback.selectionChanged(); } catch (_) { /* no haptics */ } },
  ok() { try { atLeast("6.1") && W.HapticFeedback.notificationOccurred("success"); } catch (_) { /* no haptics */ } },
  bump() { try { atLeast("6.1") && W.HapticFeedback.impactOccurred("heavy"); } catch (_) { /* no haptics */ } },
};

export function initData() { return (W && W.initData) || ""; }

export function startParam() {
  const p = new URLSearchParams(location.search);
  return (W && W.initDataUnsafe && W.initDataUnsafe.start_param) || p.get("tgWebAppStartParam") || p.get("answer") || "";
}

// Location: Telegram LocationManager (Bot API 8.0) first, then the browser.
export function getLocation(timeoutMs = 8000) {
  return new Promise((resolve) => {
    const done = (v) => { clearTimeout(t); resolve(v); };
    const t = setTimeout(() => resolve(null), timeoutMs);
    const browser = () => {
      if (!navigator.geolocation) return done(null);
      navigator.geolocation.getCurrentPosition(
        (p) => done({ lat: p.coords.latitude, lon: p.coords.longitude }),
        () => done(null), { enableHighAccuracy: false, timeout: timeoutMs - 500, maximumAge: 120000 });
    };
    if (atLeast("8.0") && W.LocationManager) {
      W.LocationManager.init(() => {
        if (!W.LocationManager.isLocationAvailable) return browser();
        W.LocationManager.getLocation((loc) => (loc ? done({ lat: loc.latitude, lon: loc.longitude }) : browser()));
      });
    } else browser();
  });
}

// Privacy: round on the device before anything leaves it (~110 m north-south, ~70 m east-west here).
export const roundCoord = (x) => Math.round(x * 1000) / 1000;

// Favorites: Telegram CloudStorage (lives with the user's Telegram account, not on our server), else localStorage.
export const store = {
  get(key) {
    return new Promise((resolve) => {
      if (atLeast("6.9") && W.CloudStorage) {
        W.CloudStorage.getItem(key, (err, v) => resolve(err ? null : v || null));
      } else {
        try { resolve(localStorage.getItem(key)); } catch (_) { resolve(null); }
      }
    });
  },
  set(key, value) {
    if (atLeast("6.9") && W.CloudStorage) { W.CloudStorage.setItem(key, value); return; }
    try { localStorage.setItem(key, value); } catch (_) { /* private mode: in memory only */ }
  },
};

// Shake to surprise: Telegram Accelerometer (8.0) or the browser's DeviceMotion.
export function onShake(cb) {
  let last = 0, hits = 0;
  const feed = (x, y, z) => {
    const g = Math.sqrt(x * x + y * y + z * z);
    const now = Date.now();
    if (g > 24) {
      hits = now - last < 700 ? hits + 1 : 1;
      last = now;
      if (hits >= 2) { hits = 0; cb(); }
    }
  };
  if (atLeast("8.0") && W.Accelerometer) {
    try {
      W.Accelerometer.start({ refresh_rate: 60 });
      W.onEvent("accelerometerChanged", () => feed(W.Accelerometer.x, W.Accelerometer.y, W.Accelerometer.z));
      return;
    } catch (_) { /* fall through */ }
  }
  window.addEventListener("devicemotion", (e) => {
    const a = e.accelerationIncludingGravity;
    if (a && a.x != null) feed(a.x, a.y, a.z);
  });
}

// Compass heading in degrees (0 = north), or null when the device can't tell.
export function onHeading(cb) {
  if (atLeast("8.0") && W.DeviceOrientation) {
    try {
      W.DeviceOrientation.start({ refresh_rate: 100, need_absolute: true }, (ok) => { if (!ok) cb(null); });
      W.onEvent("deviceOrientationChanged", () => {
        const a = W.DeviceOrientation.alpha;
        if (a != null && W.DeviceOrientation.absolute) cb((360 - (a * 180) / Math.PI) % 360);
      });
      return () => W.DeviceOrientation.stop();
    } catch (_) { /* fall through */ }
  }
  const h = (e) => {
    if (e.webkitCompassHeading != null) cb(e.webkitCompassHeading);
    else if (e.absolute && e.alpha != null) cb((360 - e.alpha) % 360);
  };
  window.addEventListener("deviceorientationabsolute", h);
  window.addEventListener("deviceorientation", h);
  return () => { window.removeEventListener("deviceorientationabsolute", h); window.removeEventListener("deviceorientation", h); };
}

export function openLink(url) {
  if (W && atLeast("6.1")) { W.openLink(url); return; }
  window.open(url, "_blank", "noopener");
}

export function share(url, text) {
  const tme = `https://t.me/share/url?url=${encodeURIComponent(url)}&text=${encodeURIComponent(text)}`;
  if (W && atLeast("6.1")) { W.openTelegramLink(tme); return Promise.resolve("telegram"); }
  if (navigator.share) return navigator.share({ title: "Qydyr", text, url }).then(() => "native").catch(() => "cancel");
  return navigator.clipboard.writeText(`${text} ${url}`).then(() => "copied").catch(() => "fail");
}
