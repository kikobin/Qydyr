// Суырбек — the Qydyr mascot: a steppe marmot in an ақ қалпақ who walks you around Astana.
// One flat SVG, dressed by mood: a тымақ and a scarf in the frost, a mask in smog, an umbrella in the rain,
// sleepy eyes at night, sunglasses in summer. Animations live in app.css (.mascot …) and stop for
// prefers-reduced-motion. Original character, drawn for this project.

export const MASCOT_NAME = { ru: "Суырбек", kk: "Суырбек", en: "Suyrbek" };

const C = {
  fur: "#D29A5B", furDark: "#A86F3C", ear: "#B47A43", belly: "#F3D9AE", ink: "#3A2A20", eye: "#0B1626",
  cheek: "#F4A38C", tooth: "#FFFDF7", toothLine: "#D9C4A3", felt: "#F8F5EE", feltShade: "#E6DECC",
  velvet: "#1F1D22", gold: "#FEC50C", sky: "#00AFCA", skyDark: "#0A8DA5", furHat: "#C9772E", furLight: "#EBC99A",
  mask: "#EAF3F8", maskLine: "#B9CCD8", mouth: "#7A2E2A", tongue: "#E06A5A",
};

const MOODS = ["hello", "talk", "think", "happy", "celebrate", "cold", "smog", "rain", "night", "summer", "shrug"];

// ------------------------------------------------------------------ parts
const tail = () => `<ellipse cx="150" cy="181" rx="13" ry="7" transform="rotate(-25 150 181)" fill="${C.furDark}"/>`;
const feet = () => `<ellipse cx="80" cy="190" rx="15" ry="7" fill="${C.furDark}"/><ellipse cx="120" cy="190" rx="15" ry="7" fill="${C.furDark}"/>`;
const body = () => `<path d="M100 92C138 92 154 122 153 152C152 178 130 192 100 192C70 192 48 178 47 152C46 122 62 92 100 92Z" fill="${C.fur}"/>
  <path d="M100 118C120 118 130 136 129 156C128 175 116 186 100 186C84 186 72 175 71 156C70 136 80 118 100 118Z" fill="${C.belly}"/>`;
const ears = () => `<circle cx="61" cy="60" r="10" fill="${C.fur}"/><circle cx="61" cy="60" r="5.5" fill="${C.ear}"/>
  <circle cx="139" cy="60" r="10" fill="${C.fur}"/><circle cx="139" cy="60" r="5.5" fill="${C.ear}"/>`;
const head = () => `<ellipse cx="100" cy="78" rx="45" ry="39" fill="${C.fur}"/>
  <ellipse cx="100" cy="96" rx="23" ry="17" fill="${C.belly}"/>`;
const cheeks = (hot) => `<ellipse cx="73" cy="92" rx="7" ry="4.5" fill="${hot ? "#F27C6B" : C.cheek}" opacity="0.75"/>
  <ellipse cx="127" cy="92" rx="7" ry="4.5" fill="${hot ? "#F27C6B" : C.cheek}" opacity="0.75"/>`;
const whiskers = () => `<g fill="${C.ear}"><circle cx="88" cy="95" r="1.1"/><circle cx="86" cy="99" r="1.1"/><circle cx="112" cy="95" r="1.1"/><circle cx="114" cy="99" r="1.1"/></g>`;

function eyes(kind) {
  if (kind === "squint") return `<g stroke="${C.eye}" stroke-width="3.2" stroke-linecap="round" fill="none"><path d="M76.5 75.5Q83 79.5 89.5 78"/><path d="M110.5 78Q117 79.5 123.5 75.5"/></g>`;
  if (kind === "sleepy") return `<g stroke="${C.eye}" stroke-width="3" stroke-linecap="round" fill="none"><path d="M77 77Q83 82 89 77"/><path d="M111 77Q117 82 123 77"/></g>`;
  if (kind === "joy") return `<g stroke="${C.eye}" stroke-width="3.2" stroke-linecap="round" fill="none"><path d="M77 79Q83 71 89 79"/><path d="M111 79Q117 71 123 79"/></g>`;
  const dy = kind === "up" ? -3 : 0;
  return `<g class="m-eyes">
    <ellipse cx="83" cy="${77 + dy}" rx="7.2" ry="8.8" fill="${C.eye}"/><ellipse cx="117" cy="${77 + dy}" rx="7.2" ry="8.8" fill="${C.eye}"/>
    <circle cx="85.6" cy="${73.2 + dy}" r="2.6" fill="#fff"/><circle cx="119.6" cy="${73.2 + dy}" r="2.6" fill="#fff"/>
    <circle cx="81" cy="${80.6 + dy}" r="1.1" fill="#fff"/><circle cx="115" cy="${80.6 + dy}" r="1.1" fill="#fff"/></g>`;
}

const nose = () => `<path d="M92.5 88Q100 83.5 107.5 88Q105 95 100 96Q95 95 92.5 88Z" fill="${C.ink}"/>
  <ellipse cx="98" cy="87.4" rx="2.6" ry="1.2" fill="#fff" opacity="0.45"/>`;
const teeth = () => `<rect x="95.6" y="101" width="4.2" height="8" rx="1.2" fill="${C.tooth}" stroke="${C.toothLine}" stroke-width="0.8"/>
  <rect x="100.2" y="101" width="4.2" height="8" rx="1.2" fill="${C.tooth}" stroke="${C.toothLine}" stroke-width="0.8"/>`;

function mouth(kind) {
  const line = `<path d="M100 96V100" stroke="${C.ink}" stroke-width="2.2" stroke-linecap="round"/>`;
  if (kind === "open") return `${line}<path d="M90 100Q100 116 110 100Z" fill="${C.mouth}"/><path d="M94.5 106.5Q100 111 105.5 106.5Q100 113 94.5 106.5Z" fill="${C.tongue}"/>${teeth()}`;
  if (kind === "talk") return `${line}<ellipse class="m-talk" cx="100" cy="104" rx="5.5" ry="4.5" fill="${C.mouth}"/>${teeth()}`;
  if (kind === "flat") return `${line}<path d="M93 101Q100 103 107 101" stroke="${C.ink}" stroke-width="2.2" stroke-linecap="round" fill="none"/>${teeth()}`;
  return `${line}<path d="M91 100Q95.5 105 100 100.5Q104.5 105 109 100" stroke="${C.ink}" stroke-width="2.2" stroke-linecap="round" fill="none"/>${teeth()}`;
}

// arms
const pawsFront = () => `<ellipse cx="80" cy="131" rx="10" ry="8" transform="rotate(30 80 131)" fill="${C.fur}" stroke="${C.furDark}" stroke-width="1.2"/>
  <ellipse cx="120" cy="131" rx="10" ry="8" transform="rotate(-30 120 131)" fill="${C.fur}" stroke="${C.furDark}" stroke-width="1.2"/>`;
const pawLeftFront = () => `<ellipse cx="80" cy="131" rx="10" ry="8" transform="rotate(30 80 131)" fill="${C.fur}" stroke="${C.furDark}" stroke-width="1.2"/>`;
const armWave = () => `<g class="m-wave"><path d="M136 124C146 116 153 104 156 92" stroke="${C.fur}" stroke-width="15" stroke-linecap="round" fill="none"/>
  <circle cx="157" cy="88" r="9" fill="${C.fur}" stroke="${C.furDark}" stroke-width="1.2"/>
  <path d="M152 82l1.5-4M157 80.5v-4.2M162 82l-1.5-4" stroke="${C.furDark}" stroke-width="1.4" stroke-linecap="round"/></g>`;
const armsUp = () => `<g class="m-cheer"><path d="M64 124C54 116 47 104 44 92" stroke="${C.fur}" stroke-width="15" stroke-linecap="round" fill="none"/>
  <circle cx="43" cy="88" r="9" fill="${C.fur}" stroke="${C.furDark}" stroke-width="1.2"/>
  <path d="M136 124C146 116 153 104 156 92" stroke="${C.fur}" stroke-width="15" stroke-linecap="round" fill="none"/>
  <circle cx="157" cy="88" r="9" fill="${C.fur}" stroke="${C.furDark}" stroke-width="1.2"/></g>`;
const armChin = () => `<path d="M128 132C122 124 114 118 108 113" stroke="${C.fur}" stroke-width="15" stroke-linecap="round" fill="none"/>
  <circle cx="106" cy="112" r="8.5" fill="${C.fur}" stroke="${C.furDark}" stroke-width="1.2"/>`;
const armUmbrella = () => `<path d="M136 126C144 120 148 112 150 104" stroke="${C.fur}" stroke-width="15" stroke-linecap="round" fill="none"/>
  <circle cx="150" cy="102" r="8.5" fill="${C.fur}" stroke="${C.furDark}" stroke-width="1.2"/>`;
const shrugArms = () => `<path d="M62 126C52 124 44 118 40 110" stroke="${C.fur}" stroke-width="15" stroke-linecap="round" fill="none"/>
  <circle cx="38" cy="107" r="8.5" fill="${C.fur}" stroke="${C.furDark}" stroke-width="1.2"/>
  <path d="M138 126C148 124 156 118 160 110" stroke="${C.fur}" stroke-width="15" stroke-linecap="round" fill="none"/>
  <circle cx="162" cy="107" r="8.5" fill="${C.fur}" stroke="${C.furDark}" stroke-width="1.2"/>`;

// hats and outfits
const aqQalpaq = () => `<g transform="rotate(-6 100 50)">
  <path d="M70 53C68 30 84 8 100 4C116 8 132 30 130 53Z" fill="${C.felt}"/>
  <path d="M117 10C127 22 131.5 36 130 53H121C121 38 120 24 117 10Z" fill="${C.feltShade}"/>
  <path d="M80 33Q100 27 120 33" stroke="${C.feltShade}" stroke-width="1.6" fill="none" stroke-dasharray="2 3"/>
  <path d="M53 47C70 50 86 46 96.5 35L98.5 62C82 64.5 66 62 55 57Z" fill="${C.velvet}"/>
  <path d="M147 47C130 50 114 46 103.5 35L101.5 62C118 64.5 134 62 145 57Z" fill="${C.velvet}"/>
  <path d="M53 47C70 50 86 46 96.5 35M147 47C130 50 114 46 103.5 35" stroke="${C.gold}" stroke-width="1.3" fill="none"/>
  <g stroke="${C.gold}" stroke-width="1.5" stroke-linecap="round" fill="none">
    <path d="M63 55c1.5-4 7-4.5 8-.5c.8 3-3 4.2-4.2 2"/><path d="M78 56c1.5-4 7-4.5 8-.5c.8 3-3 4.2-4.2 2"/>
    <path d="M137 55c-1.5-4-7-4.5-8-.5c-.8 3 3 4.2 4.2 2"/><path d="M122 56c-1.5-4-7-4.5-8-.5c-.8 3 3 4.2 4.2 2"/></g></g>`;
const tymaq = () => `<path d="M58 54C49 72 51 98 62 111C70 102 72 82 73 60Z" fill="${C.furHat}" stroke="${C.furLight}" stroke-width="3"/>
  <path d="M142 54C151 72 149 98 138 111C130 102 128 82 127 60Z" fill="${C.furHat}" stroke="${C.furLight}" stroke-width="3"/>
  <path d="M66 50C66 26 84 9 100 8C116 9 134 26 134 50Z" fill="${C.sky}"/>
  <path d="M100 8V50M83 14Q78 30 78 50M117 14Q122 30 122 50" stroke="${C.skyDark}" stroke-width="1.6" fill="none"/>
  <path d="M69 46Q100 39 131 46" stroke="${C.gold}" stroke-width="2.4" fill="none"/>
  <path d="M57 56C77 43 123 43 143 56C141 65 59 65 57 56Z" fill="${C.furLight}"/>
  <path d="M66 54l3 4M76 50l2 5M88 48l1 5M100 47v5M112 48l-1 5M124 50l-2 5M134 54l-3 4" stroke="#D9AE77" stroke-width="1.6" stroke-linecap="round"/>
  <circle cx="100" cy="8.5" r="4" fill="${C.gold}"/>`;
const scarf = () => `<path d="M60 108C79 121 121 121 140 108L142 119C121 132 79 132 58 119Z" fill="${C.gold}"/>
  <path d="M70 116.5Q100 126 130 116.5" stroke="${C.sky}" stroke-width="2.4" fill="none"/>
  <path d="M118 121L127 153L115 155L109 125Z" fill="${C.gold}"/><path d="M114.5 135L124.5 133" stroke="${C.sky}" stroke-width="2.4"/>`;
const puff = () => `<g class="m-puff" fill="#fff"><circle cx="126" cy="101" r="4" opacity="0.7"/><circle cx="135" cy="95" r="5" opacity="0.55"/><circle cx="145" cy="88" r="6" opacity="0.4"/></g>`;
const maskPart = () => `<path d="M62 84L79 91M138 84L121 91" stroke="${C.maskLine}" stroke-width="1.8" stroke-linecap="round"/>
  <path d="M78 89Q100 83 122 89L120 106Q100 113 80 106Z" fill="${C.mask}" stroke="${C.maskLine}" stroke-width="1.4"/>
  <path d="M82 95Q100 91 118 95M82 100Q100 97 118 100" stroke="${C.maskLine}" stroke-width="1.2" fill="none"/>`;
const sunglasses = () => `<path d="M72 71H94V80Q94 87 87 87H79Q72 87 72 80Z" fill="${C.eye}"/>
  <path d="M106 71H128V80Q128 87 121 87H113Q106 87 106 80Z" fill="${C.eye}"/>
  <path d="M94 74Q100 71 106 74" stroke="${C.eye}" stroke-width="2.4" fill="none"/>
  <path d="M76 74L82 74M110 74L116 74" stroke="${C.sky}" stroke-width="2" stroke-linecap="round"/>`;
const umbrella = () => `<path d="M150 104V34" stroke="${C.ink}" stroke-width="2.6"/>
  <path d="M112 40Q150 2 188 40Q180 35 172 40Q164 35 156 40Q148 35 140 40Q132 35 124 40Q118 36 112 40Z" fill="${C.sky}"/>
  <path d="M150 7Q140 20 140 40M150 7Q160 20 160 40" stroke="${C.skyDark}" stroke-width="1.4" fill="none"/>
  <g class="m-rain" stroke="#8FD3E6" stroke-width="2" stroke-linecap="round"><path d="M28 30l-3 9M44 52l-3 9M22 70l-3 9M38 92l-3 9M178 64l-3 9M186 92l-3 9"/></g>`;
const moon = () => `<path d="M168 22A16 16 0 1 0 184 44A12.5 12.5 0 1 1 168 22Z" fill="${C.gold}"/>
  <g fill="#EDF2F7"><circle cx="146" cy="16" r="1.8"/><circle cx="186" cy="12" r="1.4"/><circle cx="158" cy="52" r="1.3"/></g>
  <g class="m-zz" fill="${C.sky}" font-size="0"><path d="M136 30h8l-8 8h8" stroke="${C.sky}" stroke-width="2" fill="none" stroke-linejoin="round"/></g>`;
const dots = () => `<g class="m-dots" fill="${C.sky}"><circle cx="146" cy="44" r="3.2"/><circle cx="157" cy="33" r="4.2"/><circle cx="170" cy="20" r="5.4"/></g>`;
const sparkles = () => `<g class="m-spark" fill="${C.gold}"><path d="M30 60l3 7 7 3-7 3-3 7-3-7-7-3 7-3z"/><path d="M172 54l2.4 5.6 5.6 2.4-5.6 2.4-2.4 5.6-2.4-5.6-5.6-2.4 5.6-2.4z"/></g>`;
const confetti = () => `<g class="m-confetti">
  <rect x="26" y="40" width="7" height="12" rx="2" fill="${C.sky}" transform="rotate(-24 29 46)"/>
  <rect x="168" y="36" width="7" height="12" rx="2" fill="${C.gold}" transform="rotate(28 171 42)"/>
  <rect x="18" y="104" width="6" height="10" rx="2" fill="#FF9A8E" transform="rotate(40 21 109)"/>
  <rect x="178" y="104" width="6" height="10" rx="2" fill="${C.sky}" transform="rotate(-36 181 109)"/>
  <circle cx="48" cy="22" r="3.5" fill="${C.gold}"/><circle cx="154" cy="16" r="3.5" fill="#FF9A8E"/>
  <circle cx="186" cy="76" r="3" fill="${C.gold}"/><circle cx="14" cy="78" r="3" fill="${C.sky}"/></g>`;
const question = () => `<path d="M168 36Q168 26 177 26Q186 26 186 34Q186 40 179 43V48" stroke="${C.sky}" stroke-width="4" stroke-linecap="round" fill="none"/><circle cx="179" cy="57" r="2.8" fill="${C.sky}"/>`;

// ------------------------------------------------------------------ assembly
const SPEC = {
  hello: { arms: "wave", eyes: "open", mouth: "smile", hat: "qalpaq" },
  talk: { arms: "front", eyes: "open", mouth: "talk", hat: "qalpaq" },
  think: { arms: "chin", eyes: "up", mouth: "flat", hat: "qalpaq", extra: "dots" },
  happy: { arms: "up", eyes: "joy", mouth: "open", hat: "qalpaq", extra: "sparkles" },
  celebrate: { arms: "up", eyes: "joy", mouth: "open", hat: "qalpaq", extra: "confetti" },
  cold: { arms: "front", eyes: "open", mouth: "flat", hat: "tymaq", scarf: true, extra: "puff", shiver: true, hot: true },
  smog: { arms: "front", eyes: "squint", mouth: "none", hat: "qalpaq", mask: true },
  rain: { arms: "umbrella", eyes: "open", mouth: "smile", hat: "qalpaq", extra: "umbrella" },
  night: { arms: "front", eyes: "sleepy", mouth: "smile", hat: "qalpaq", extra: "moon" },
  summer: { arms: "wave", eyes: "open", mouth: "smile", hat: "qalpaq", glasses: true },
  shrug: { arms: "shrug", eyes: "open", mouth: "flat", hat: "qalpaq", extra: "question" },
};

export function mascot(mood = "hello", { size = 96, title = "", still = false, cls = "" } = {}) {
  const s = SPEC[mood] || SPEC.hello;
  const arms = { wave: pawLeftFront() + armWave(), up: armsUp(), chin: pawLeftFront() + armChin(), umbrella: pawLeftFront() + armUmbrella(),
    shrug: shrugArms(), front: pawsFront() }[s.arms];
  const behind = s.extra === "umbrella" ? umbrella() : "";
  const extra = { dots: dots(), sparkles: sparkles(), confetti: confetti(), puff: puff(), moon: moon(), question: question() }[s.extra] || "";
  const face = `${cheeks(s.hot)}${s.glasses ? sunglasses() : eyes(s.eyes)}${nose()}${s.mask ? maskPart() : mouth(s.mouth)}${s.mask ? "" : whiskers()}`;
  const label = title ? `<title>${title}</title>` : "";
  return `<svg class="mascot m-${mood}${still ? " still" : ""}${cls ? ` ${cls}` : ""}" viewBox="0 0 200 200" width="${size}" height="${size}" role="img" aria-label="${title || MASCOT_NAME.ru}" xmlns="http://www.w3.org/2000/svg">${label}
  ${behind}<g class="m-bob${s.shiver ? " m-shiver" : ""}">${tail()}${feet()}${body()}${ears()}${head()}${face}${s.hat === "tymaq" ? tymaq() : aqQalpaq()}${s.scarf ? scarf() : ""}${arms}</g>${extra}</svg>`;
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
