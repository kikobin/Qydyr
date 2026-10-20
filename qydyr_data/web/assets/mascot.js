// Суырбек — the Qydyr mascot: a steppe marmot in a тақия with a red bag who walks you around Astana.
// Drawn art (assets/art/suyrbek-*.webp, rendered from the v5 design's vector poses): one pose per mood.
// The bot sends the same character as 512×512 stickers (assets/mascot/<mood>.webp).

export const MASCOT_NAME = { ru: "Суырбек", kk: "Суырбек", en: "Suyrbek" };

const MOODS = ["hello", "talk", "think", "happy", "celebrate", "cold", "smog", "rain", "night", "summer", "shrug"];

// mood -> the drawn pose; the art has six, the weather moods share them
const POSE = {
  hello: "wave", talk: "friendly", think: "thinking", happy: "friendly", celebrate: "wave", cold: "winter",
  smog: "thinking", rain: "winter", night: "friendly", summer: "wave", shrug: "shrug", map: "map",
};
const RATIO = { winter: 0.711, wave: 0.776, shrug: 0.834, map: 0.627, thinking: 0.654, friendly: 0.691 };   // width / height

export function mascot(mood = "hello", { size = 96, title = "", still = false, cls = "" } = {}) {
  const pose = POSE[mood] || "wave";
  const w = Math.round(size * RATIO[pose]);
  return `<img class="mascot m-${mood}${still ? " still" : ""}${cls ? ` ${cls}` : ""}" src="assets/art/suyrbek-${pose}.webp" width="${w}" height="${size}" alt="${title || MASCOT_NAME.ru}" draggable="false" decoding="async">`;
}

// What to wear for the weather: a /weather reply ({mood} already decided by the server) or an answer's
// context ({weather, air}). Smog > frost > rain > night > summer.
export function moodForWeather(w, air) {
  if (!w) return "hello";
  if (w.mood) return w.mood;
  const aqi = air ? (air.european_aqi ?? air.aqi) : (w.air ? (w.air.european_aqi ?? w.air.aqi) : null);
  if (aqi != null && aqi >= 80) return "smog";
  const feels = w.feels_c ?? w.feels ?? w.temp_c ?? w.temp;
  if (feels != null && feels <= -10) return "cold";
  const snow = [71, 73, 75, 77, 85, 86].includes(w.code);
  if (w.precip && !snow) return "rain";
  if (w.dark) return "night";
  if (feels != null && feels >= 24) return "summer";
  return "hello";
}

export { MOODS };
