// Sound effects, synthesized live with the Web Audio API: no audio files, works offline.
// Browsers only allow audio after the first click or key press, so the context starts then.
import { store } from "./core.js";

let ctx = null;
let enabled = store.get("sound", true);
const VOLUME = 0.06;

function audio() {
  if (!ctx) {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    ctx = new AC();
  }
  if (ctx.state === "suspended") ctx.resume();
  return ctx;
}
for (const ev of ["pointerdown", "keydown"]) window.addEventListener(ev, () => enabled && audio(), { once: true });

function tone(freq, start, dur, { type = "sine", vol = 1, to = null } = {}) {
  const a = audio();
  if (!a) return;
  const t = a.currentTime + start;
  const o = a.createOscillator(), g = a.createGain();
  o.type = type;
  o.frequency.setValueAtTime(freq, t);
  if (to) o.frequency.exponentialRampToValueAtTime(to, t + dur);
  g.gain.setValueAtTime(0.0001, t);
  g.gain.exponentialRampToValueAtTime(VOLUME * vol, t + 0.01);
  g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
  o.connect(g).connect(a.destination);
  o.start(t); o.stop(t + dur + 0.02);
}

const SOUNDS = {
  click: () => tone(1800, 0, 0.03, { type: "square", vol: 0.25 }),
  start: () => { tone(320, 0, 0.18, { type: "sawtooth", vol: 0.35, to: 960 }); tone(1280, 0.16, 0.08, { vol: 0.5 }); },
  route: () => { tone(660, 0, 0.06, { type: "triangle" }); tone(990, 0.07, 0.06, { type: "triangle" }); },
  work: () => tone(2400, 0, 0.025, { type: "square", vol: 0.15 }),
  backup: () => { tone(500, 0, 0.07, { type: "triangle" }); tone(500, 0.1, 0.07, { type: "triangle" }); },
  done: () => { tone(880, 0, 0.07, { type: "triangle", vol: 0.8 }); tone(1320, 0.07, 0.1, { type: "triangle", vol: 0.8 }); },
  fail: () => tone(180, 0, 0.22, { type: "sawtooth", vol: 0.5, to: 90 }),
  final: () => [523, 659, 784, 1046].forEach((f, i) => tone(f, i * 0.07, 0.35, { type: "sine", vol: 0.7 })),
  error: () => { tone(220, 0, 0.15, { type: "square", vol: 0.4 }); tone(165, 0.16, 0.25, { type: "square", vol: 0.4 }); },
  prayer: () => [392, 494, 587, 784].forEach((f, i) => tone(f, i * 0.12, 0.6, { type: "sine", vol: 0.5 })),
};

export function play(name) {
  if (enabled && SOUNDS[name]) { try { SOUNDS[name](); } catch { /* audio is a nicety */ } }
}
export function setSound(on) { enabled = on; store.set("sound", on); if (on) audio(); }
export const soundOn = () => enabled;
