// Start-up sequence: Nathan's boot screen, about three seconds. Click or press any key
// to skip; skipped entirely for people who prefer reduced motion.
import { $ } from "./core.js";
import { play } from "./sound.js";

const LINES = [
  "NATHAN OS v{v} // cold start",
  "mounting free tiers ............ ok",
  "waking the staff ............... {n} models",
  "loading Elo ladder ............. ok",
  "calibrating sarcasm ............ 94%",
  "consulting Paul Morphy ......... he moved first",
  "lightning capacitors ........... charged",
  "all systems nominal. probably.",
];

export function boot(status) {
  const box = $("boot");
  if (!box || matchMedia("(prefers-reduced-motion: reduce)").matches) { box?.remove(); return Promise.resolve(); }
  const log = box.querySelector(".boot-log");
  return new Promise((done) => {
    let i = 0, finished = false;
    const finish = () => {
      if (finished) return; finished = true;
      box.classList.add("out"); play("final");
      setTimeout(() => { box.remove(); done(); }, 600);
    };
    box.addEventListener("click", finish); addEventListener("keydown", finish, { once: true });
    const next = () => {
      if (finished) return;
      if (i >= LINES.length) { box.classList.add("ready"); setTimeout(finish, 700); return; }
      const line = document.createElement("div");
      line.textContent = LINES[i++].replace("{v}", status?.version || "1").replace("{n}", status?.staff_ready ?? "?");
      log.append(line); play("work");
      setTimeout(next, 210 + Math.random() * 160);
    };
    setTimeout(next, 450);
  });
}
