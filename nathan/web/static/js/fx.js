// Lightning: jagged, branching bolts drawn on a full-screen canvas above the page.
// Off for anyone who prefers reduced motion, and switchable with the ⚡ toggle.
import { store } from "./core.js";
import { play } from "./sound.js";

let canvas, ctx, enabled = store.get("lightning", true);
const reduced = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const bolts = [];

export const lightningOn = () => enabled && !reduced();
export function setLightning(on) { enabled = on; store.set("lightning", on); }

function ensure() {
  if (canvas) return;
  canvas = document.createElement("canvas");
  canvas.className = "fx";
  canvas.setAttribute("aria-hidden", "true");
  document.body.append(canvas);
  ctx = canvas.getContext("2d");
  const size = () => {
    const r = window.devicePixelRatio || 1;
    canvas.width = innerWidth * r; canvas.height = innerHeight * r;
    ctx.setTransform(r, 0, 0, r, 0, 0);
  };
  size(); addEventListener("resize", size);
}

// Midpoint displacement: split the line, push the middle sideways, repeat.
function path(x1, y1, x2, y2, jag) {
  let pts = [[x1, y1], [x2, y2]];
  for (let d = jag; d > 2; d /= 2) {
    const next = [pts[0]];
    for (let i = 1; i < pts.length; i++) {
      const [ax, ay] = pts[i - 1], [bx, by] = pts[i];
      const len = Math.hypot(bx - ax, by - ay) || 1;
      const off = (Math.random() - 0.5) * d;
      next.push([(ax + bx) / 2 + (-(by - ay) / len) * off, (ay + by) / 2 + ((bx - ax) / len) * off], pts[i]);
    }
    pts = next;
  }
  return pts;
}

function bolt(x1, y1, x2, y2, width, depth = 0) {
  const pts = path(x1, y1, x2, y2, Math.hypot(x2 - x1, y2 - y1) / 4);
  bolts.push({ pts, width, born: performance.now() });
  if (depth < 2) {  // a few forks peel off and fizzle out
    for (let i = 0; i < 2 + Math.random() * 2; i++) {
      const [bx, by] = pts[Math.floor(pts.length * (0.2 + Math.random() * 0.6))];
      const ang = Math.atan2(y2 - y1, x2 - x1) + (Math.random() - 0.5) * 1.6;
      const len = Math.hypot(x2 - x1, y2 - y1) * (0.15 + Math.random() * 0.2);
      bolt(bx, by, bx + Math.cos(ang) * len, by + Math.sin(ang) * len, width * 0.45, depth + 1);
    }
  }
}

const LIFE = 420;
function frame(now) {
  ctx.clearRect(0, 0, innerWidth, innerHeight);
  for (let i = bolts.length - 1; i >= 0; i--) {
    const b = bolts[i], age = now - b.born;
    if (age > LIFE) { bolts.splice(i, 1); continue; }
    const a = 1 - age / LIFE, flicker = age < 90 ? 1 : 0.55 + Math.random() * 0.45;
    for (const [w, color] of [[b.width * 6, `rgba(63,211,255,${0.18 * a})`], [b.width * 2.2, `rgba(140,230,255,${0.55 * a * flicker})`],
      [b.width, `rgba(255,255,255,${a * flicker})`]]) {
      ctx.beginPath(); ctx.moveTo(...b.pts[0]);
      for (const p of b.pts.slice(1)) ctx.lineTo(...p);
      ctx.strokeStyle = color; ctx.lineWidth = w; ctx.lineJoin = "round"; ctx.lineCap = "round"; ctx.stroke();
    }
  }
  if (bolts.length) requestAnimationFrame(frame); else ctx.clearRect(0, 0, innerWidth, innerHeight);
}

function flash() {
  document.body.classList.remove("flash");
  void document.body.offsetWidth;  // restart the CSS animation
  document.body.classList.add("flash");
}

// Strike a point (or an element's centre) from somewhere above it.
export function strike(target, { width = 2.4, big = false } = {}) {
  if (!lightningOn()) return;
  ensure();
  let x, y;
  if (target instanceof Element) {
    const r = target.getBoundingClientRect();
    if (r.bottom < 0 || r.top > innerHeight) return;  // off screen: no strike
    x = r.left + r.width / 2; y = r.top + Math.min(r.height / 2, 60);
  } else [x, y] = target;
  const fromX = x + (Math.random() - 0.5) * innerWidth * 0.5;
  const starting = bolts.length === 0;
  bolt(fromX, -10, x, y, big ? width * 1.6 : width);
  if (big) { flash(); play("thunder"); }
  if (starting) requestAnimationFrame(frame);
}

export function doubleStrike(target) {
  strike(target, { big: true });
  setTimeout(() => strike(target, { width: 1.8 }), 140);
}
