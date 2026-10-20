// Line icons (stroke = currentColor). No emoji anywhere in the interface.
const s = (body, cls = "i") =>
  `<svg class="${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;

export const LOGO = `<svg class="logo" viewBox="0 0 24 24" fill="none" stroke="var(--sky)" stroke-width="1.6" aria-hidden="true"><circle cx="12" cy="12" r="9.5"/><circle cx="12" cy="12" r="3.2"/><path d="M4.2 7.5C8 10 16 10 19.8 7.5M4.2 16.5C8 14 16 14 19.8 16.5"/><path d="M8.8 2.9C10.5 7 10.5 17 8.8 21.1M15.2 2.9C13.5 7 13.5 17 15.2 21.1"/><circle cx="12" cy="2.5" r="1.4" fill="#FEC50C" stroke="none"/></svg>`;

export const I = {
  pin: (c) => s(`<path d="M12 21s-7-6.2-7-11.5A7 7 0 0 1 19 9.5C19 14.8 12 21 12 21z"/><circle cx="12" cy="9.5" r="2.5"/>`, c),
  mic: (c) => s(`<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/>`, c),
  send: (c) => s(`<path d="M4 12l16-8-6 16-2-6z"/>`, c),
  route: (c) => s(`<path d="M3 11l18-8-8 18-2-8z"/>`, c),
  share: (c) => s(`<circle cx="18" cy="5" r="2.6"/><circle cx="6" cy="12" r="2.6"/><circle cx="18" cy="19" r="2.6"/><path d="M8.4 13.3l7.2 4.4M15.6 6.3l-7.2 4.4"/>`, c),
  ask: (c) => s(`<path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1.1-4.6A8 8 0 1 1 21 12z"/><path d="M9.6 9.6a2.4 2.4 0 0 1 4.5 1.2c0 1.6-2.1 1.8-2.1 3.1M12 16.7h.01"/>`, c),
  chev: (c) => s(`<path d="M9 6l6 6-6 6"/>`, c),
  back: (c) => s(`<path d="M15 6l-6 6 6 6"/>`, c),
  close: (c) => s(`<path d="M6 6l12 12M18 6L6 18"/>`, c),
  plus: (c) => s(`<path d="M12 5v14M5 12h14"/>`, c),
  check: (c) => s(`<path d="M5 12l5 5 9-10"/>`, c),
  clock: (c) => s(`<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>`, c),
  walk: (c) => s(`<circle cx="13" cy="4" r="2"/><path d="M10 21l1.5-6L14 17v4M8.5 11.5L11 8l3 .5 2.5 3M11 8l-.8 5"/>`, c),
  hourglass: (c) => s(`<path d="M7 3h10M7 21h10M8 3c0 5 8 6 8 9s-8 4-8 9M16 3c0 5-8 6-8 9s8 4 8 9"/>`, c),
  target: (c) => s(`<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3"/>`, c),
  snow: (c) => s(`<path d="M12 2v20M4.9 6l14.2 12M19.1 6L4.9 18M9 3.5l3 2 3-2M9 20.5l3-2 3 2"/>`, c),
  kid: (c) => s(`<circle cx="12" cy="6" r="3"/><path d="M7 21v-5l-2-3 4-2h6l4 2-2 3v5"/>`, c),
  wheel: (c) => s(`<circle cx="12" cy="4" r="2"/><path d="M12 7v6h5l2 5M12 10H8"/><path d="M8 13a5 5 0 1 0 7 6"/>`, c),
  plane: (c) => s(`<path d="M21 15l-8-4V5a1.5 1.5 0 0 0-3 0v6l-8 4v2l8-2v4l-2 1.5V22l3.5-1 3.5 1v-1.5L13 19v-4l8 2z"/>`, c),
  event: (c) => s(`<rect x="4" y="5" width="16" height="15" rx="2"/><path d="M8 3v4M16 3v4M4 10h16"/>`, c),
  speaker: (c) => s(`<path d="M4 10v4h4l5 4V6L8 10z"/><path d="M16.5 8.5a5 5 0 0 1 0 7M19 6a8.5 8.5 0 0 1 0 12"/>`, c),
  bookmark: (c) => s(`<path d="M6 3h12v18l-6-4-6 4z"/>`, c),
  dice: (c) => s(`<rect x="4" y="4" width="16" height="16" rx="4"/><circle cx="9" cy="9" r="1.2" fill="currentColor"/><circle cx="15" cy="15" r="1.2" fill="currentColor"/><circle cx="15" cy="9" r="1.2" fill="currentColor"/><circle cx="9" cy="15" r="1.2" fill="currentColor"/>`, c),
  sliders: (c) => s(`<path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>`, c),
  globe: (c) => s(`<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>`, c),
  locate: (c) => s(`<circle cx="12" cy="12" r="4"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/>`, c),
  map: (c) => s(`<path d="M9 4L3 6v14l6-2 6 2 6-2V4l-6 2z"/><path d="M9 4v14M15 6v14"/>`, c),
  list: (c) => s(`<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>`, c),
  sun: (c) => s(`<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>`, c),
  moon: (c) => s(`<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>`, c),
  camera: (c) => s(`<path d="M4 8h3l2-3h6l2 3h3v11H4z"/><circle cx="12" cy="13" r="3.5"/>`, c),
  people: (c) => s(`<circle cx="9" cy="8" r="3"/><path d="M3 20a6 6 0 0 1 12 0"/><circle cx="17" cy="9" r="2.5"/><path d="M16 14a5 5 0 0 1 5 5"/>`, c),
  reset: (c) => s(`<path d="M4 4v5h5"/><path d="M5.1 13.5A7 7 0 1 0 6.3 7L4 9"/>`, c),
  thumbUp: (c) => s(`<path d="M7 11v9H4v-9zM7 11l4-8a2 2 0 0 1 2 2v4h5.5a2 2 0 0 1 2 2.3l-1.2 7A2 2 0 0 1 17.3 20H7"/>`, c),
  thumbDown: (c) => s(`<path d="M17 13V4h3v9zM17 13l-4 8a2 2 0 0 1-2-2v-4H5.5a2 2 0 0 1-2-2.3l1.2-7A2 2 0 0 1 6.7 4H17"/>`, c),
  arrow: (c) => `<svg class="${c || "i"}" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 2l6 18-6-4-6 4z"/></svg>`,
  play: (c) => s(`<path d="M7 5l12 7-12 7z"/>`, c),
  flag: (c) => s(`<path d="M5 21V4M5 4h11l-2 4 2 4H5"/>`, c),
  link: (c) => s(`<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>`, c),
  // v5 design: the tab bar, tiles, notes
  home: (c) => s(`<path d="M3 10.5L12 3l9 7.5V21h-6v-6H9v6H3z"/>`, c),
  star: (c) => s(`<path d="M12 3l2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1-4.4-4.3 6.1-.9z"/>`, c),
  shield: (c) => s(`<path d="M12 3l8 3v6c0 4.6-3.6 8-8 9.5C7.6 20 4 16.6 4 12V6z"/><path d="M9 12l2 2 4-4"/>`, c),
  lock: (c) => s(`<rect x="5" y="10.5" width="14" height="10" rx="2"/><path d="M8.5 10.5V7a3.5 3.5 0 0 1 7 0v3.5"/>`, c),
  search: (c) => s(`<circle cx="10.5" cy="10.5" r="7"/><path d="M16 16l5 5"/>`, c),
  warning: (c) => s(`<path d="M12 3l9.5 17h-19z"/><path d="M12 10v4.5M12 17.5h.01"/>`, c),
  coffee: (c) => s(`<path d="M4 9h12v5a6 6 0 0 1-12 0zM16 10h1.5a2.5 2.5 0 0 1 0 5H16M7 3.5c-1 1 1 2 0 3M11 3.5c-1 1 1 2 0 3"/>`, c),
  music: (c) => s(`<path d="M9 18V5l11-2v13"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="17.5" cy="16" r="2.5"/>`, c),
};

// reason codes of "почему не это" -> icon
export const REASON_ICON = {
  far: "walk", cold_walk: "snow", flight: "plane", closed: "clock", short: "hourglass", wait: "clock",
  weather: "snow", kids: "kid", wheelchair: "wheel", fit: "target", outranked: "target", not_event: "event",
};

// the asyq (knucklebone) thrown by "Удиви меня"
export const ASYQ = `<svg class="asyq" viewBox="0 0 120 120" aria-hidden="true"><defs><linearGradient id="ag" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#FFF4D6"/><stop offset="1" stop-color="#E8C779"/></linearGradient></defs><path d="M28 46c-8-14 6-28 20-22 6-10 22-10 28 0 14-6 28 8 20 22 10 6 10 22 0 28 8 14-6 28-20 22-6 10-22 10-28 0-14 6-28-8-20-22-10-6-10-22 0-28z" fill="url(#ag)" stroke="#8A6A1F" stroke-width="2.5"/><path d="M44 52c8 6 24 6 32 0M44 68c8-6 24-6 32 0" fill="none" stroke="#8A6A1F" stroke-width="2.5" stroke-linecap="round"/><circle cx="60" cy="60" r="5" fill="#C49A3A"/></svg>`;
