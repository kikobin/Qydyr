// Weather over the map for the time & weather machine: snow, rain, smog haze, a warm summer evening.
export class WeatherFx {
  constructor(canvas) {
    this.c = canvas;
    this.ctx = canvas.getContext("2d");
    this.mode = null;
    this.parts = [];
    this.raf = null;
    // A canvas is a replaced element: without an explicit CSS size it stays at its intrinsic 300x150,
    // so the size comes from the parent box and is re-read whenever that box changes.
    if (window.ResizeObserver) new ResizeObserver(() => this.resize()).observe(canvas.parentElement || canvas);
    else window.addEventListener("resize", () => this.resize());
    this.resize();
  }

  resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const box = (this.c.parentElement || this.c).getBoundingClientRect();
    this.w = Math.max(1, Math.round(box.width));
    this.h = Math.max(1, Math.round(box.height));
    this.c.width = this.w * dpr;
    this.c.height = this.h * dpr;
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  set(mode) {
    if (mode === this.mode) return;
    this.mode = mode;
    cancelAnimationFrame(this.raf);
    this.ctx.clearRect(0, 0, this.w, this.h);
    this.c.style.background = "";
    if (!mode || window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      if (mode === "smog") this.c.style.background = "rgba(150,140,120,0.22)";
      if (mode === "summer_evening") this.c.style.background = "rgba(255,170,90,0.10)";
      return;
    }
    const n = { blizzard: 260, frost: 40, rain: 180, smog: 70, summer_evening: 26 }[mode] || 0;
    this.parts = Array.from({ length: n }, () => this.spawn(true));
    const loop = () => { this.frame(); this.raf = requestAnimationFrame(loop); };
    this.raf = requestAnimationFrame(loop);
  }

  spawn(anywhere) {
    const m = this.mode;
    const x = Math.random() * this.w, y = anywhere ? Math.random() * this.h : -10;
    if (m === "rain") return { x, y, v: 9 + Math.random() * 6, l: 10 + Math.random() * 12 };
    if (m === "smog") return { x, y, r: 40 + Math.random() * 90, v: 0.15 + Math.random() * 0.3, a: 0.04 + Math.random() * 0.05 };
    if (m === "summer_evening") return { x, y, r: 1 + Math.random() * 1.8, v: 0.2 + Math.random() * 0.3, a: Math.random() * Math.PI * 2 };
    const big = m === "blizzard";
    return { x, y, r: 0.6 + Math.random() * (big ? 2.4 : 1.4), v: (big ? 1.4 : 0.4) + Math.random() * (big ? 2.6 : 0.8), drift: big ? -2.2 - Math.random() * 2 : -0.3 };
  }

  frame() {
    const { ctx, w, h, mode } = this;
    ctx.clearRect(0, 0, w, h);
    if (mode === "smog") { ctx.fillStyle = "rgba(160,150,130,0.16)"; ctx.fillRect(0, 0, w, h); }
    if (mode === "summer_evening") {
      const g = ctx.createLinearGradient(0, 0, 0, h);
      g.addColorStop(0, "rgba(255,150,80,0.16)"); g.addColorStop(1, "rgba(255,150,80,0)");
      ctx.fillStyle = g; ctx.fillRect(0, 0, w, h);
    }
    for (let i = 0; i < this.parts.length; i++) {
      const p = this.parts[i];
      if (mode === "rain") {
        ctx.strokeStyle = "rgba(190,215,240,0.45)"; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(p.x, p.y); ctx.lineTo(p.x - 2, p.y + p.l); ctx.stroke();
        p.y += p.v; p.x -= 0.6;
      } else if (mode === "smog") {
        const g = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, p.r);
        g.addColorStop(0, `rgba(170,160,140,${p.a})`); g.addColorStop(1, "rgba(170,160,140,0)");
        ctx.fillStyle = g; ctx.beginPath(); ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2); ctx.fill();
        p.x += p.v;
        if (p.x - p.r > w) { p.x = -p.r; }
      } else if (mode === "summer_evening") {
        p.a += 0.03;
        ctx.fillStyle = `rgba(255,230,140,${0.35 + 0.35 * Math.sin(p.a)})`;
        ctx.beginPath(); ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2); ctx.fill();
        p.y -= p.v * 0.3; p.x += Math.sin(p.a) * 0.3;
      } else {
        ctx.fillStyle = "rgba(237,242,247,0.85)";
        ctx.globalAlpha = 0.35 + (p.r / 3);
        ctx.beginPath(); ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2); ctx.fill();
        ctx.globalAlpha = 1;
        p.y += p.v; p.x += p.drift;
      }
      if (p.y > h + 20 || p.x < -40 || p.y < -60) this.parts[i] = this.spawn(false);
    }
  }
}
