// Evolve: Nathan improves its own code. Ask for a change, let it pick one, or put it on autopilot.
import { $, api, el, toast } from "./core.js";
import { play } from "./sound.js";

const LIVE = new Set(["starting", "working", "testing"]);
const LABEL = { starting: "starting", working: "building", testing: "testing", ready: "ready to apply",
  failed: "didn't pass", applied: "applied", discarded: "discarded" };
let timer = null, openId = null, wired = false;

const ago = (t) => { const s = Math.round(Date.now() / 1000 - t); return s < 60 ? `${s}s` : s < 3600 ? `${Math.round(s / 60)}m` : `${Math.round(s / 3600)}h`; };

async function act(path, body = {}, okText = "") {
  try { const r = await api(path, body); if (okText) toast(okText); play("click"); return r; }
  catch (e) { toast(e.message, 6000); play("error"); return null; }
}

function wire() {
  if (wired) return; wired = true;
  $("ev-go").onclick = async () => {
    const goal = $("ev-goal").value.trim();
    if (!goal) { toast("Describe the change, or press “Let Nathan choose”."); $("ev-goal").focus(); return; }
    if (await act("/api/evolve", { goal }, "On it: building on a separate copy.")) { $("ev-goal").value = ""; refresh(); }
  };
  $("ev-auto").onclick = async () => { if (await act("/api/evolve", { goal: "" }, "Nathan is picking an improvement.")) refresh(); };
  $("ev-pilot").onclick = async () => {
    const rounds = Number($("ev-rounds").value);
    if (await act("/api/evolve", { goal: $("ev-goal").value.trim(), rounds },
      `Autopilot: ${rounds} rounds. Changes that pass the tests are applied automatically.`)) refresh();
  };
  $("ev-stop").onclick = async () => { await act("/api/evolve-autopilot/stop", {}, "Autopilot stops after this round."); refresh(); };
  $("ev-restart").onclick = async () => {
    if (!confirm("Rebuild Nathan with the applied changes and reopen it? This takes about a minute.")) return;
    if (await act("/api/evolve-restart", {})) { $("bye").hidden = false; $("bye").querySelector("p").textContent = "Rebuilding with the new changes… Nathan reopens by itself in about a minute."; }
  };
  $("ev-goal").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) $("ev-go").click(); });
}

async function detail(job) {
  const full = await api(`/api/evolve/${job.id}`).catch(() => null);
  if (!full) return el("div");
  const actions = el("div", { class: "row" });
  if (full.status === "ready") actions.append(
    el("button", { class: "go small", onclick: async () => { if (await act(`/api/evolve/${job.id}/apply`, {}, "Applied and committed to your source.")) refresh(); } }, "Apply"));
  if (!["applied", "discarded"].includes(full.status)) actions.append(
    el("button", { class: "mini", onclick: async () => { if (await act(`/api/evolve/${job.id}/discard`, {}, "Discarded.")) refresh(); } }, LIVE.has(full.status) ? "Stop & discard" : "Discard"));
  return el("div", { class: "ev-detail" },
    actions,
    full.stat ? el("pre", { class: "report" }, full.stat) : "",
    full.diff ? el("details", {}, el("summary", {}, "Show the full change"), diffView(full.diff)) : "",
    full.tests ? el("details", {}, el("summary", {}, full.passed ? "Test report (passed)" : "Test report (failed)"), el("pre", { class: "report" }, full.tests)) : "",
    el("details", { open: LIVE.has(full.status) }, el("summary", {}, "Work log"), el("pre", { class: "report ev-log" }, full.log.join("\n") || "…")));
}

function diffView(diff) {
  const pre = el("pre", { class: "report diff" });
  for (const line of diff.split("\n")) {
    const cls = line.startsWith("+") && !line.startsWith("+++") ? "add" : line.startsWith("-") && !line.startsWith("---") ? "del" : line.startsWith("@@") ? "hunk" : "";
    pre.append(el("span", { class: cls }, line + "\n"));
  }
  return pre;
}

async function refresh() {
  const s = await api("/api/evolve").catch(() => null);
  if (!s) return;
  $("ev-source").textContent = s.ready ? `Source: ${s.source}` : "Nathan's source code wasn't found on this computer, so it can't change itself here.";
  for (const id of ["ev-go", "ev-auto", "ev-pilot"]) $(id).disabled = !s.ready || s.running || s.autopilot.on;
  $("ev-stop").hidden = !s.autopilot.on;
  $("ev-pilot-note").textContent = s.autopilot.on ? `Autopilot round ${s.autopilot.round}/${s.autopilot.rounds} · ${s.autopilot.applied} applied` : "";
  $("ev-restart-row").hidden = !s.pending_restart;
  $("ev-restart").textContent = s.frozen ? "Rebuild & restart" : "Restart to load";
  $("ev-restart").disabled = !s.frozen;
  const list = $("ev-jobs"); list.textContent = "";
  if (!s.jobs.length) list.append(el("p", { class: "muted" }, "No improvements yet. Ask for one above, or let Nathan choose."));
  for (const j of s.jobs) {
    const head = el("button", { class: "ev-head", "aria-expanded": String(openId === j.id),
      onclick: () => { openId = openId === j.id ? null : j.id; refresh(); } },
      el("span", { class: `ev-badge ${j.status}` }, LABEL[j.status] || j.status),
      el("b", {}, j.summary || j.goal || "Nathan's choice"),
      el("span", { class: "muted" }, `${j.manager || "…"} · ${ago(j.started)} ago`));
    const card = el("article", { class: "panel ev-job" }, head);
    if (LIVE.has(j.status) && j.log.length) card.append(el("div", { class: "muted ev-last" }, j.log[j.log.length - 1]));
    if (openId === j.id) card.append(await detail(j));
    list.append(card);
  }
  clearTimeout(timer);
  if (!$("view-evolve").hidden) timer = setTimeout(refresh, s.running || s.autopilot.on ? 2500 : 10000);
}

export function openEvolve() { wire(); refresh(); }
