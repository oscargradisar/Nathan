// The console: one command bar, every mode, live cards for every model on the job.
import { $, api, clock, el, follow, md, plain, provider, short, store, toast } from "./core.js";
import { boot, errorLine, finalLine, quip, startLine } from "./quips.js";
import { play } from "./sound.js";
import { canListen, listen, say, stop as stopSpeech } from "./voice.js";
import { doubleStrike, strike } from "./fx.js";

export const MODES = [
  { id: "auto", label: "Auto", help: "Nathan decides: a quick model for small things, the whole company for big ones." },
  { id: "single", label: "Quick", help: "One fast model. For when you just need an answer." },
  { id: "research", label: "Research", help: "Searches Wikipedia, papers, forums and the web, then several models answer from the sources with citations and a verifier cross-checks." },
  { id: "workflow", label: "Workflow", help: "A manager splits it into parts, workers do them in parallel, a writer joins them, a reviewer checks." },
  { id: "vote", label: "Council", help: "Five models answer separately; a judge merges the best of each." },
  { id: "swarm", label: "Swarm", help: "Everyone available answers at once, then a judge merges. Loud, thorough, free." },
  { id: "critique", label: "Critique", help: "One model drafts, another tears it apart, a third rewrites." },
  { id: "agent", label: "Agent", help: "Can use your workspace: read and write files, run Python, fetch pages, query CSV and SQLite data." },
];

const TICKS = 36;
const units = new Map();      // id -> unit state + elements
let mode = store.get("mode", "auto");
let run = null, stopFollow = null, elo = {}, prayerMessage = "", lastAnswer = "";
let contributors = new Set();
let onFinish = () => {};

export function setStaff(rows) { elo = Object.fromEntries(rows.map((r) => [r.ref, r])); }
export function onRunFinished(fn) { onFinish = fn; }

export function log(text, tone = "sys") {
  const feed = $("feed");
  feed.append(el("li", { class: tone }, el("time", {}, clock()), text));
  while (feed.children.length > 300) feed.firstChild.remove();
  feed.scrollTop = feed.scrollHeight;
}
const say_ = (line) => { if (line) log(line[0], line[1]); };

// ---- setup ---------------------------------------------------------------------------
export function initConsole(staffCount) {
  const box = $("modes");
  for (const m of MODES) {
    box.append(el("button", { type: "button", class: "mode", role: "radio", "aria-checked": String(m.id === mode),
      "data-mode": m.id, title: m.help, onclick: () => setMode(m.id) }, m.label));
  }
  setMode(mode, true);
  const g = $("ticks");
  for (let i = 0; i < TICKS; i++) {
    const a = (i / TICKS) * Math.PI * 2 - Math.PI / 2;
    const [x1, y1, x2, y2] = [100 + Math.cos(a) * 40, 100 + Math.sin(a) * 40, 100 + Math.cos(a) * 50, 100 + Math.sin(a) * 50];
    g.append(svg("line", { x1, y1, x2, y2 }));
  }
  $("cmd").addEventListener("submit", (e) => { e.preventDefault(); start(); });
  $("prompt").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); start(); }
  });
  $("copy").onclick = async () => { try { await navigator.clipboard.writeText(lastAnswer); toast("Copied."); } catch { toast("Couldn't copy: select the text instead."); } };
  $("speak").onclick = () => say(lastAnswer, { force: true });
  $("approve").onclick = approvePrayer;
  $("deliver").onclick = deliverPrayer;
  $("clear-log").onclick = () => { $("feed").textContent = ""; };
  const mic = $("mic");
  if (!canListen()) mic.hidden = true;
  let stopMic = null;
  mic.onclick = () => {
    if (stopMic) { stopMic(); return; }
    mic.classList.add("on"); play("click");
    stopMic = listen((text) => { $("prompt").value = text; start(); },
      () => { mic.classList.remove("on"); stopMic = null; });
  };
  document.body.classList.add("idle");
  say_(boot(staffCount));
  setInterval(tickTimers, 1000);
}

function setMode(id, quiet = false) {
  mode = id; store.set("mode", id);
  for (const b of $("modes").children) b.setAttribute("aria-checked", String(b.dataset.mode === id));
  $("mode-help").textContent = MODES.find((m) => m.id === id)?.help || "";
  if (!quiet) play("click");
}

export function ask(text, as) {
  const keep = mode;
  $("prompt").value = text;
  if (as) setMode(as, true);
  location.hash = "#console";
  start();  // reads `mode` before its first await
  if (as && !MODES.some((m) => m.id === as)) setMode(keep, true);  // Code/Review aren't chips: don't stick
}

// ---- running ----------------------------------------------------------------------------
async function start() {
  const prompt = $("prompt").value.trim();
  if (!prompt || $("go").disabled) return;
  stopFollow && stopFollow(); stopSpeech();
  reset();
  busy(true, "ROUTING", mode === "auto" ? "deciding how to handle this" : (MODES.find((m) => m.id === mode)?.label || { code: "programming", review: "performance review" }[mode] || mode).toLowerCase());
  play("start"); say_(startLine());
  strike($("core"), { big: true });
  try {
    run = await api("/api/runs", { prompt, mode, n: 5 });
  } catch (e) {
    busy(false); play("error"); say_(errorLine(e.message)); toast(e.message); return;
  }
  stopFollow = follow(run.id, 0, handle);
}

function sources(items) {
  const old = $("sources"); if (old) old.remove();
  if (!items.length) return;
  const ol = el("ol");
  for (const s of items) ol.append(el("li", {}, el("a", { href: s.url, target: "_blank", rel: "noopener" }, s.title || s.url),
    el("span", { class: "src" }, s.source)));
  $("units").after(el("div", { class: "panel sources", id: "sources" }, el("header", {}, el("h2", {}, `Sources · ${items.length}`)), ol));
}

function reset() {
  units.clear(); contributors = new Set();
  const src = $("sources"); if (src) src.remove();
  for (const n of $("answer").querySelectorAll(".row")) n.remove();
  $("units").textContent = ""; $("route").hidden = true;
  for (const id of ["answer", "review", "prayer"]) $(id).hidden = true;
  $("approve-note").textContent = ""; $("prayer-reply").value = "";
  for (const t of $("ticks").children) t.setAttribute("class", "");
}

function busy(on, big = "", small = "") {
  document.body.classList.toggle("busy", on);
  document.body.classList.toggle("idle", !on);
  $("go").disabled = on;
  $("core-big").textContent = on ? big : "STANDING BY";
  $("core-small").textContent = on ? small : "ask anything";
}

const DOING = { answerer: "ANSWERING", drafter: "DRAFTING", critic: "CRITIQUING", reviser: "REVISING",
  racer: "RACING", writer: "WRITING", reviewer: "REVIEWING", scout: "SCOUTING", source: "SEARCHING",
  analyst: "ANALYSING", verifier: "VERIFYING", "premium researcher": "RESEARCHING", coder: "CODING",
  manager: "MANAGING", "assistant manager": "REVIEWING", "research director": "REVIEWING" };

function handle(ev) {
  say_(quip(ev));
  const role = ev.type === "working" || (ev.type === "step" && ev.status === "working") ? ev.role : null;
  if (role && DOING[role]) { $("core-big").textContent = DOING[role]; $("core-small").textContent = short(ev.worker || ev.model); }
  else if (role && role.startsWith("tool")) { $("core-big").textContent = "USING TOOLS"; $("core-small").textContent = role.slice(6); }
  switch (ev.type) {
    case "routed": {
      const r = $("route"); r.hidden = false;
      r.textContent = `→ ${label(ev.pattern)}  ·  ${ev.task_type}${ev.reason ? "  ·  " + ev.reason : ""}`;
      $("core-small").textContent = label(ev.pattern).toLowerCase();
      play("route"); break;
    }
    case "phase": phase(ev.phase); break;
    case "plan":
      unit("manager", { role: "manager", task: "Split your request into parts", model: ev.planner, status: "done",
        text: ev.tasks.map((t) => `${t.id}: ${t.instruction}`).join("\n") });
      for (const t of ev.tasks) unit(t.id, { role: "part " + t.id, task: t.instruction, model: t.worker,
        status: "queued", state: t.depends_on.length ? "waits for " + t.depends_on.join(", ") : "assigned" });
      $("core-small").textContent = `${ev.tasks.length} parts · ${new Set(ev.tasks.map((t) => t.worker)).size} workers`;
      play("route"); break;
    case "working":
      if (ev.role === "patient") break;
      unit(ev.id, { role: roleOf(ev), model: ev.worker, status: "working", start: Date.now() }); play("work"); break;
    case "backup": {
      const u = unit(ev.id, {});
      u.backups = (u.backups || []).concat(ev.worker);
      addLog(u, `backup ${short(ev.worker)} joins (${short(ev.for)} is slow)`);
      paint(u); play("backup"); strike(u.els.card); break;
    }
    case "reassigned":
      if (ev.role === "patient") break;
      { const u = unit(ev.id, { model: ev.worker, status: "working", start: Date.now() });
        addLog(u, `${short(ev.from)} didn't deliver (${ev.reason}) → ${short(ev.worker)}`); }
      play("fail"); break;
    case "delivered":
      if (ev.role === "patient") break;
      unit(ev.id, { model: ev.worker, status: "done", text: ev.text, backups: [],
        state: ev.seconds != null ? `done · ${ev.seconds}s` : "done" });
      if (["worker", "writer", "analyst", "premium researcher", "verifier", "coder"].includes(ev.role)) contributors.add(ev.worker);
      play("done"); break;
    case "task_failed":
      if (ev.role === "patient") break;
      unit(ev.id, { status: "failed", state: ev.reason || "nobody could do it" }); play("fail"); break;
    case "redo":
      for (const id of ev.ids) {
        const u = unit(id, { status: "queued", state: `redo (scored ${ev.scores[id]}/10)` });
        addLog(u, "reviewer sent it back"); strike(u.els.card);
      }
      play("backup"); break;
    case "review": review(ev); break;
    case "ratings":
      for (const [ref, d] of Object.entries(ev.changes))
        for (const u of units.values()) if (u.model === ref && u.status === "done") { u.delta = d; paint(u); }
      break;
    case "prayer":
      $("prayer").hidden = false; $("prayer-text").textContent = ev.text; prayerMessage = ev.message;
      play("prayer"); break;
    case "step": {
      const st = { working: "working", done: "done", skipped: "skipped" }[ev.status] || ev.status;
      unit(ev.id, { role: ev.role, model: ev.model, status: st, text: ev.text || undefined,
        state: st === "done" ? `done${ev.seconds != null ? " · " + ev.seconds + "s" : ""}` : st === "skipped" ? "lost the race" : undefined,
        start: st === "working" ? Date.now() : undefined });
      if (st === "done") { contributors.add(ev.model); play("done"); } else if (st === "working") play("work");
      break;
    }
    case "lanes":
      ev.lanes.forEach((l) => unit("lane" + l.id, { role: "voter " + (l.id + 1), model: l.model, status: "working", start: Date.now(), text: "" }));
      $("core-big").textContent = ev.lanes.length > 5 ? "SWARM" : "COUNCIL";
      $("core-small").textContent = `${ev.lanes.length} models answering`;
      break;
    case "delta": { const u = units.get("lane" + ev.lane); if (u) { u.text = (u.text || "") + ev.text; paintText(u); } break; }
    case "lane_switch": { const u = unit("lane" + ev.lane, { model: ev.model, text: "", start: Date.now() }); addLog(u, `switched to ${short(ev.model)} (${ev.reason})`); break; }
    case "lane_done":
      unit("lane" + ev.lane, ev.ok ? { status: "done", state: `done · ${(ev.ms / 1000).toFixed(1)}s`, model: ev.model }
        : { status: "failed", state: ev.error || "failed" });
      if (ev.ok) { contributors.add(ev.model); play("done"); } else play("fail");
      break;
    case "judge_start":
      unit("judge", { role: "judge", task: "Merge the best of every answer", model: ev.model, status: "working", start: Date.now(), text: "" });
      $("core-big").textContent = "JUDGING"; break;
    case "judge_delta": { const u = units.get("judge"); if (u) { u.text += ev.text; paintText(u); } break; }
    case "judge_failed": { const u = unit("judge", {}); addLog(u, `${short(ev.model)} couldn't judge (${ev.reason})`); break; }
    case "manual_added": unit("manual" + units.size, { role: "pasted", model: ev.model, status: "done", text: ev.text }); break;
    case "sources": sources(ev.items); break;
    case "saved": {
      const a = $("answer");
      const note = el("div", { class: "row" }, el("span", { class: "tag" }, `saved ${ev.files.length} file(s)`),
        el("button", { class: "mini", onclick: () => api("/api/open-workspace", {}) }, "Open workspace"));
      a.append(note); play("done"); break;
    }
    case "final": case "blessed": {
      const j = units.get("judge");
      if (j && j.status === "working") unit("judge", { status: "done", state: "merged", model: ev.judge || j.model });
      answer(ev); break;
    }
    case "error": play("error"); say_(errorLine(plain(ev.message))); toast(plain(ev.message), 5000); break;
    case "done": busy(false); onFinish(); break;
  }
  paintRing();
}

function phase(p) {
  const names = { planning: ["PLANNING", "manager is splitting the job"], working: ["WORKING", "parts in progress"],
    writing: ["WRITING", "joining it all up"], reviewing: ["REVIEWING", "asked vs got"], fixing: ["FIXING", "redoing weak parts"],
    scouting: ["SCOUTING", "deciding what to search"], searching: ["SEARCHING", "six sources at once"],
    analysing: ["ANALYSING", "reading the evidence"], verifying: ["VERIFYING", "cross-checking claims"] };
  if (names[p]) { $("core-big").textContent = names[p][0]; $("core-small").textContent = names[p][1]; }
}

function answer(ev) {
  const a = $("answer"); a.hidden = false;
  lastAnswer = ev.text;
  $("answer-body").innerHTML = md(ev.text);
  const by = ev.writer || ev.judge;
  const k = Math.max(contributors.size, by ? 1 : 0);
  $("answer-meta").textContent = (ev.type === "blessed" ? "revised with Claude's word · " : "")
    + (by ? "by " + short(by) : (ev.models || []).map(short).join(", ") || "") + (k > 1 ? ` · ${k} models` : "");
  if (ev.type === "final") { say_(finalLine(k || 1)); play("final"); }
  requestAnimationFrame(() => doubleStrike($("answer")));
  say(ev.text);
}

function review(ev) {
  $("review").hidden = false;
  $("review-meta").textContent = "by " + short(ev.reviewer);
  $("review-summary").textContent = ev.summary;
  const fill = (id, items, empty) => { const ul = $(id); ul.textContent = ""; (items.length ? items : [empty]).forEach((x) => ul.append(el("li", {}, x))); };
  fill("review-delivered", ev.delivered || [], "—");
  fill("review-missing", ev.missing || [], "nothing important");
  for (const [id, s] of Object.entries(ev.scores || {})) { const u = units.get(id); if (u) { u.score = s; paint(u); } }
}

async function approvePrayer() {
  try { await navigator.clipboard.writeText(prayerMessage); $("approve-note").textContent = "Copied. Paste it into Claude, then bring the reply back."; }
  catch { $("approve-note").textContent = "Couldn't copy automatically: select the prayer above."; }
  window.open("https://claude.ai/new", "_blank");
}

async function deliverPrayer() {
  const text = $("prayer-reply").value.trim();
  if (!run || !text) { toast("Paste Claude's reply first."); return; }
  try {
    const r = await api(`/api/runs/${run.id}/prayer`, { source: "claude.ai", text });
    $("prayer").hidden = true; busy(true, "REVISING", "applying Claude's word");
    stopFollow = follow(run.id, r.since, handle);
  } catch (e) { toast(e.message); }
}

// ---- units -------------------------------------------------------------------------------
function roleOf(ev) { return ev.role === "worker" ? "part " + ev.id : ev.role; }
function label(p) { return MODES.find((m) => m.id === p)?.label || p; }

function unit(id, patch) {
  let u = units.get(id);
  if (!u) {
    u = { id, role: patch.role || id, status: "queued", log: [], tick: units.size % TICKS };
    u.els = {
      card: el("div", { class: "unit" }),
      role: el("span", { class: "u-role" }), state: el("span", { class: "u-state" }),
      task: el("div", { class: "u-task" }), model: el("div", { class: "u-model" }),
      log: el("div", { class: "u-log" }), text: el("div", { class: "u-text", title: "click to expand" }),
      bar: el("div", { class: "u-bar" }),
    };
    const E = u.els;
    E.text.onclick = () => E.text.classList.toggle("open");
    E.card.append(el("div", { class: "u-top" }, E.role, E.state), E.task, E.model, E.log, E.text, E.bar);
    $("units").append(E.card);
    units.set(id, u);
  }
  for (const [k, v] of Object.entries(patch)) if (v !== undefined) u[k] = v;
  if (patch.status === "working" && patch.state === undefined) u.state = undefined;  // show the live timer
  paint(u);
  return u;
}

function addLog(u, line) { u.log.push(line); paint(u); }

function paint(u) {
  const E = u.els;
  E.card.className = "unit " + u.status;
  E.role.textContent = u.role;
  E.task.textContent = u.task || ""; E.task.hidden = !u.task;
  const r = elo[u.model];
  E.model.textContent = "";
  if (u.model) {
    E.model.append(`${short(u.model)} · ${provider(u.model)}`);
    if (r) E.model.append(el("span", { class: "elo" }, ` · ${r.rank} ${r.elo}`));
    if (u.delta != null) E.model.append(el("span", { class: u.delta >= 0 ? "ok-t" : "bad-t" }, ` ${u.delta >= 0 ? "+" : ""}${u.delta} Elo`));
    if (u.backups && u.backups.length) {
      const n = u.backups.length;
      E.model.append(el("span", { class: "elo", title: u.backups.map(short).join(", ") }, ` + ${n} backup${n > 1 ? "s" : ""}`));
    }
  }
  if (u.score != null) E.role.textContent = `${u.role} · ${u.score}/10`;
  E.log.textContent = ""; u.log.slice(-3).forEach((l) => E.log.append(el("div", {}, l)));
  E.state.textContent = u.state || (u.status === "working" ? elapsed(u) : u.status);
  paintText(u);
}
function paintText(u) {
  const t = u.els.text;
  t.textContent = u.text || ""; t.hidden = !u.text;
  if (u.status === "working" && u.text) t.scrollTop = t.scrollHeight;
}
function elapsed(u) { return u.start ? `working · ${Math.round((Date.now() - u.start) / 1000)}s` : "working"; }
function tickTimers() {
  for (const u of units.values()) if (u.status === "working" && !u.state) u.els.state.textContent = elapsed(u);
}

function paintRing() {
  const ticks = $("ticks").children;
  for (const t of ticks) t.setAttribute("class", "");
  let i = 0;
  for (const u of units.values()) {
    const cls = { working: "lit", done: "done", failed: "fail" }[u.status];
    const t = ticks[i++ % TICKS];
    if (cls) t.setAttribute("class", cls);
  }
}

function svg(tag, attrs) {
  const e = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  return e;
}
