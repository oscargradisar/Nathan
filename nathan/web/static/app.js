// Nathan control room. Plain JS, no build step.
"use strict";
const $ = (id) => document.getElementById(id);
const api = (path, body) => fetch(path, body === undefined ? {} : {
  method: "POST", headers: { "content-type": "application/json", "x-nathan": "1" },
  body: JSON.stringify(body),
}).then(async (r) => { const j = await r.json().catch(() => ({})); if (!r.ok) throw new Error(j.detail || r.status); return j; });

let current = null;     // {id, manual_prompt}
let failed = false;     // an error event arrived for the current run
let source = null;      // EventSource
const laneEls = new Map();

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) k === "class" ? (e.className = v) : e.setAttribute(k, v);
  for (const kid of kids) e.append(kid);
  return e;
}

function addLane(id, model) {
  const badge = el("span", { class: "badge run" }, "streaming");
  const name = el("span", {}, model);
  const body = el("div", { class: "lane-body" });
  const card = el("div", { class: "card" }, el("h3", {}, name, badge), body);
  $("lanes").append(card);
  laneEls.set(id, { card, badge, name, body });
}

function setBadge(b, cls, text) { b.className = "badge " + cls; b.textContent = text; }

function handle(ev) {
  const lane = laneEls.get(ev.lane);
  switch (ev.type) {
    case "lanes": ev.lanes.forEach((l) => addLane(l.id, l.model)); break;
    case "delta": lane.body.textContent += ev.text; lane.body.scrollTop = lane.body.scrollHeight; break;
    case "lane_switch":
      lane.name.textContent = ev.model; lane.body.textContent = "";
      setBadge(lane.badge, "run", `switched (${ev.reason})`); break;
    case "lane_done":
      setBadge(lane.badge, ev.ok ? "ok" : "bad", ev.ok ? `${ev.ms} ms` : ev.error); break;
    case "subtask_start":
      addLane(ev.id, `${ev.id}: ${ev.instruction.slice(0, 80)}`); break;
    case "subtask_done": {
      const l = laneEls.get(ev.id);
      l.body.textContent = ev.text || ev.error;
      setBadge(l.badge, ev.error ? "bad" : "ok", ev.model || "failed"); break;
    }
    case "judge_start":
      $("final").hidden = false; $("final-body").textContent = "";
      setBadge($("judge"), "run", "judge " + ev.model); break;
    case "judge_delta": $("final-body").textContent += ev.text; break;
    case "judge_failed": setBadge($("judge"), "bad", `judge ${ev.model} failed`); break;
    case "manual_added":
      addLane("m" + laneEls.size, ev.model);
      { const l = laneEls.get("m" + (laneEls.size - 1)); l.body.textContent = ev.text; setBadge(l.badge, "ok", "pasted"); }
      break;
    case "final":
      $("final").hidden = false; $("final-body").textContent = ev.text;
      setBadge($("judge"), "ok", ev.judge ? "judge " + ev.judge : (ev.models || []).join(", ") || "answer");
      $("note").textContent = ev.note || ""; break;
    case "error": failed = true; $("status").textContent = ev.message; break;
    case "done":
      $("run").disabled = false;
      if (!failed) $("status").textContent = "done";
      source.close(); loadQuota(); break;
  }
}

function listen(id, since = 0) {
  source = new EventSource(`/api/runs/${id}/events?since=${since}`);
  source.onmessage = (m) => handle(JSON.parse(m.data));
}

async function run() {
  const prompt = $("prompt").value.trim();
  if (!prompt) return;
  source && source.close();
  $("lanes").textContent = ""; laneEls.clear();
  $("final").hidden = true; $("note").textContent = "";
  $("status").textContent = "running…"; $("run").disabled = true; failed = false;
  try {
    current = await api("/api/runs", { prompt, mode: $("mode").value, n: Number($("n").value) });
  } catch (e) { $("status").textContent = e.message; $("run").disabled = false; return; }
  $("manual").hidden = $("mode").value !== "vote";
  $("sites").textContent = "";
  for (const [name, url] of Object.entries(current.sites))
    $("sites").append(el("a", { href: url, target: "_blank", rel: "noopener" }, "open " + name));
  listen(current.id);
}

async function addManual() {
  const text = $("manual-text").value.trim();
  if (!current || !text) return;
  try {
    await api(`/api/runs/${current.id}/manual`, { source: $("manual-source").value, text });
    const r = await api(`/api/runs/${current.id}/rejudge`, {});
    $("manual-text").value = ""; $("status").textContent = "re-judging…"; failed = false;
    listen(current.id, r.since - 1);  // include the manual_added event
  } catch (e) { $("status").textContent = e.message; }
}

async function loadQuota() {
  const q = await fetch("/api/quota").then((r) => r.json());
  const box = $("quota"); box.textContent = "";
  for (const p of q.providers) {
    const total = p.used + p.remaining;
    const pct = p.unknown || !total ? 100 : Math.round((p.remaining / total) * 100);
    const label = !p.has_key ? "not connected" : p.unknown && !p.remaining ? `${p.used} used · limit unknown` : `${p.remaining} left · ${p.used} used`;
    box.append(el("div", { class: "quota-row" },
      el("div", {}, el("strong", {}, p.provider), " ", el("span", { class: "muted" }, label)),
      el("div", { class: "bar" }, Object.assign(el("span"), { style: `width:${p.has_key ? pct : 0}%` }))));
  }
}

async function init() {
  const m = await fetch("/api/models").then((r) => r.json());
  for (const mode of m.modes) $("mode").append(el("option", mode === "vote" ? { selected: "" } : {}, mode));
  $("run").onclick = run;
  $("prompt").onkeydown = (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) run(); };
  $("copy-prompt").onclick = () => navigator.clipboard.writeText(current ? current.manual_prompt : "");
  $("manual-add").onclick = addManual;
  $("quota-refresh").onclick = loadQuota;
  loadQuota();
  setInterval(loadQuota, 10000);  // also tells the desktop app this window is still open
}
init();
