// Workflow page: watch the manager split the prompt, workers do the parts, the writer and
// reviewer finish. Plain JS, no build step.
"use strict";
const $ = (id) => document.getElementById(id);
const api = (path, body) => fetch(path, body === undefined ? {} : {
  method: "POST", headers: { "content-type": "application/json", "x-nathan": "1" },
  body: JSON.stringify(body),
}).then(async (r) => { const j = await r.json().catch(() => ({})); if (!r.ok) throw new Error(j.detail || r.status); return j; });

let run = null, source = null, prayerMessage = "", elo = {};
const cards = new Map();
const short = (ref) => ref ? ref.split("/").slice(1).join("/") || ref : "";
const who = (ref) => ref ? `${short(ref)} · ${ref.split("/")[0]}${elo[ref] ? ` · ${elo[ref].rank} ${elo[ref].elo}` : ""}` : "";

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) k === "class" ? (e.className = v) : e.setAttribute(k, v);
  for (const kid of kids) e.append(kid);
  return e;
}
function badge(b, cls, text) { b.className = "badge " + cls; b.textContent = text; }

function phase(name) {
  for (const li of $("pipeline").children) {
    const order = ["planning", "working", "writing", "reviewing", "done"];
    const i = order.indexOf(li.dataset.phase), cur = order.indexOf(name);
    li.className = i < cur ? "done" : i === cur ? "active" : "";
  }
}

function addTask(t) {
  const state = el("span", { class: "badge" }, t.depends_on.length ? `waits for ${t.depends_on.join(", ")}` : "assigned");
  const worker = el("div", { class: "muted worker" }, who(t.worker));
  const log = el("div", { class: "muted log" });
  const body = el("div", { class: "lane-body" });
  const card = el("div", { class: "card task" },
    el("h3", {}, el("span", {}, t.id), state), el("div", { class: "instruction" }, t.instruction), worker, log, body);
  $("tasks").append(card);
  cards.set(t.id, { card, state, worker, log, body });
}

function handle(ev) {
  const c = cards.get(ev.id);
  switch (ev.type) {
    case "phase": phase(ev.phase); if (ev.phase !== "done") $("status").textContent = ev.phase + "…"; break;
    case "plan":
      $("who-planning").textContent = short(ev.planner);
      ev.tasks.forEach(addTask);
      $("who-working").textContent = `${new Set(ev.tasks.map((t) => t.worker)).size} workers`; break;
    case "working":
      c.worker.textContent = who(ev.worker); badge(c.state, "run", "working…"); c.card.classList.add("busy"); break;
    case "reassigned":
      c.log.append(el("div", {}, `${short(ev.from)} didn't deliver (${ev.reason}), handed to ${short(ev.to)}`)); break;
    case "delivered":
      c.card.classList.remove("busy"); c.body.textContent = ev.text; badge(c.state, "ok", `done in ${ev.seconds}s`); break;
    case "task_failed":
      c.card.classList.remove("busy"); badge(c.state, "bad", "nobody could do it"); break;
    case "final":
    case "blessed":
      $("final").hidden = false; $("final-body").textContent = ev.text;
      $("who-writing").textContent = short(ev.writer);
      badge($("writer"), "ok", (ev.type === "blessed" ? "revised with Claude's word · " : "") + "by " + short(ev.writer)); break;
    case "review":
      $("review").hidden = false; $("who-reviewing").textContent = short(ev.reviewer);
      badge($("reviewer"), "", "by " + short(ev.reviewer));
      $("summary").textContent = ev.summary;
      $("delivered").textContent = ""; $("missing").textContent = "";
      ev.delivered.forEach((d) => $("delivered").append(el("li", {}, d)));
      (ev.missing.length ? ev.missing : ["nothing important"]).forEach((m) => $("missing").append(el("li", {}, m)));
      for (const [id, score] of Object.entries(ev.scores)) {
        const t = cards.get(id); if (t) t.card.querySelector("h3").append(el("span", { class: "badge" }, `${score}/10`));
      } break;
    case "ratings":
      for (const [ref, d] of Object.entries(ev.changes)) for (const t of cards.values())
        if (t.worker.textContent.startsWith(short(ref)))
          t.worker.append(el("span", { class: d >= 0 ? "ok-text" : "bad-text" }, ` ${d >= 0 ? "+" : ""}${d} Elo`));
      break;
    case "prayer":
      $("prayer").hidden = false; $("prayer-text").textContent = ev.text; prayerMessage = ev.message; break;
    case "error": $("status").textContent = ev.message; break;
    case "done":
      $("run").disabled = false;
      if (!$("status").textContent.includes("rror")) $("status").textContent = "done";
      source.close(); loadDuty(); break;
  }
}

function listen(id, since = 0) {
  source = new EventSource(`/api/runs/${id}/events?since=${since}`);
  source.onmessage = (m) => handle(JSON.parse(m.data));
}

async function start() {
  const prompt = $("prompt").value.trim();
  if (!prompt) return;
  source && source.close();
  $("tasks").textContent = ""; cards.clear();
  for (const id of ["final", "review", "prayer"]) $(id).hidden = true;
  for (const id of ["who-planning", "who-working", "who-writing", "who-reviewing", "copied"]) $(id).textContent = "";
  $("run").disabled = true; $("status").textContent = "starting…";
  try { run = await api("/api/runs", { prompt, mode: "workflow" }); }
  catch (e) { $("status").textContent = e.message; $("run").disabled = false; return; }
  listen(run.id);
}

async function deliver() {
  const text = $("reply").value.trim();
  if (!run || !text) return;
  try {
    const r = await api(`/api/runs/${run.id}/prayer`, { source: "claude.ai", text });
    $("status").textContent = "the writer is revising with Claude's word…"; $("prayer").hidden = true;
    listen(run.id, r.since);
  } catch (e) { $("status").textContent = e.message; }
}

async function loadDuty() {
  const staff = (await fetch("/api/staff").then((r) => r.json())).staff;
  elo = Object.fromEntries(staff.map((s) => [s.ref, s]));
  const box = $("duty"); box.textContent = "";
  const working = staff.filter((s) => s.working);
  box.append(el("div", { class: "muted" }, `${staff.length} on staff · ${working.length} working now`));
  for (const s of [...working, ...staff.filter((x) => !x.working)].slice(0, 12))
    box.append(el("div", { class: "duty-row" + (s.working ? " busy" : "") },
      el("span", { class: "dot" }), el("span", {}, short(s.ref)), el("span", { class: "muted" }, ` ${s.rank} ${s.elo}`)));
}

$("run").onclick = start;
$("prompt").onkeydown = (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) start(); };
$("approve").onclick = async () => {
  try { await navigator.clipboard.writeText(prayerMessage); $("copied").textContent = "prayer copied: paste it into Claude"; }
  catch { $("copied").textContent = "couldn't copy: select the prayer above and copy it"; }
  window.open("https://claude.ai/new", "_blank");
};
$("deliver").onclick = deliver;
loadDuty();
setInterval(loadDuty, 3000);  // live "on duty" list; also tells the desktop app the window is open
