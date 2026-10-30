// Evolve: Nathan improves its own code. Ask for a change, let it pick one, ask Claude for
// ideas, or put it on autopilot.
import { $, api, el, toast } from "./core.js";
import { play } from "./sound.js";

const LIVE = new Set(["starting", "working", "testing"]);
const LABEL = { starting: "starting", working: "building", testing: "testing", ready: "ready to apply",
  failed: "didn't pass", conflict: "conflicts", applied: "applied", discarded: "discarded" };
let timer = null, openId = null, wired = false, refreshSeq = 0, ideasSig = "", wasSuggesting = false;
// Cards are kept and updated in place, not rebuilt on every poll: rebuilding would snap shut
// the diff you opened, reset the log's scroll and download the whole diff again each tick.
const cards = new Map();  // job id -> { card, head, badge, title, meta, last, detail: { sig, node } }

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
  $("ev-suggest").onclick = async () => {
    if (await act("/api/evolve-suggest", {}, "Claude is reading the code for ideas. This takes a few minutes.")) refresh();
  };
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

// ---- ideas ---------------------------------------------------------------------------------

async function startIdea(i) {
  const goal = i.why ? `${i.title}. ${i.why}` : i.title;
  if (await act("/api/evolve", { goal, idea: i.id }, "On it: building on a separate copy.")) refresh();
}

function renderIdeas(s) {
  const box = $("ev-ideas");
  const blocked = !s.ready || s.running || s.autopilot.on;
  box.hidden = !s.ideas.length;
  const sig = JSON.stringify([s.ideas.map((i) => i.id), blocked]);
  if (sig === ideasSig) return;  // unchanged: leave the cards alone
  ideasSig = sig;
  box.replaceChildren(
    el("div", { class: "ev-ideas-head" }, el("h2", {}, `Ideas · ${s.ideas.length}`),
      el("span", { class: "muted" }, "Most valuable first. Autopilot builds these in order before choosing on its own.")),
    ...s.ideas.map((i, n) => el("article", { class: "panel ev-idea" },
      el("div", { class: "ev-idea-top" },
        el("span", { class: "ev-rank" }, String(n + 1).padStart(2, "0")),
        el("span", { class: `ev-badge kind-${i.kind}` }, i.kind),
        el("span", { class: "ev-badge" }, i.size),
        el("b", {}, i.title),
        el("button", { class: "go small", disabled: blocked, onclick: () => startIdea(i) }, "Build this")),
      i.why ? el("p", { class: "muted ev-why" }, i.why) : "")));
}

// ---- jobs ----------------------------------------------------------------------------------

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
    full.diff ? el("details", { "data-k": "diff" }, el("summary", {}, "Show the full change"), diffView(full.diff)) : "",
    full.tests ? el("details", { "data-k": "tests" }, el("summary", {}, full.passed ? "Test report (passed)" : "Test report (failed)"), el("pre", { class: "report" }, full.tests)) : "",
    el("details", { "data-k": "log", open: LIVE.has(full.status) }, el("summary", {}, "Work log"), el("pre", { class: "report ev-log" }, full.log.join("\n") || "…")));
}

function diffView(diff) {
  const pre = el("pre", { class: "report diff" });
  for (const line of diff.split("\n")) {
    const cls = line.startsWith("+") && !line.startsWith("+++") ? "add" : line.startsWith("-") && !line.startsWith("---") ? "del" : line.startsWith("@@") ? "hunk" : "";
    pre.append(el("span", { class: cls }, line + "\n"));
  }
  return pre;
}

// The full job (diff, test report) only changes with its status, so it's downloaded again
// only then. While it runs, the work log is updated from the light status poll instead.
async function detailFor(c, j, seq) {
  const sig = `${j.status}|${j.passed}`;
  if (!c.detail || c.detail.sig !== sig) {
    const node = await detail(j);
    if (seq !== refreshSeq) return null;  // a newer refresh owns this card now: don't swap its node
    if (c.detail) for (const old of c.detail.node.querySelectorAll("details[data-k]")) {  // keep what you had open
      const twin = node.querySelector(`details[data-k="${old.dataset.k}"]`);
      if (twin) twin.open = old.open;
    }
    c.detail?.node.remove();
    c.detail = { sig, node };
  }
  const log = c.detail.node.querySelector(".ev-log");
  if (log && LIVE.has(j.status) && j.log.length) {
    // The light poll sends only the last 60 lines, the opened detail had them all: append
    // what's new after the line we already show, so earlier lines don't vanish
    const shown = log.textContent === "…" ? [] : log.textContent.split("\n");
    const k = shown.length ? j.log.lastIndexOf(shown[shown.length - 1]) : -1;
    const fresh = shown.length && k < 0 ? j.log : j.log.slice(k + 1);
    if (fresh.length) {
      const atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 8;
      log.textContent = shown.concat(fresh).join("\n");
      if (atEnd) log.scrollTop = log.scrollHeight;  // follow the log, unless you scrolled up to read
    }
  }
  return c.detail.node;
}

function cardFor(j) {
  let c = cards.get(j.id);
  if (!c) {
    const badge = el("span"), title = el("b"), meta = el("span", { class: "muted" });
    const head = el("button", { class: "ev-head", onclick: () => { openId = openId === j.id ? null : j.id; refresh(); } }, badge, title, meta);
    const last = el("div", { class: "muted ev-last" });
    c = { card: el("article", { class: "panel ev-job" }, head, last), head, badge, title, meta, last, detail: null };
    cards.set(j.id, c);
  }
  c.badge.className = `ev-badge ${j.status}`;
  c.badge.textContent = LABEL[j.status] || j.status;
  c.title.textContent = j.summary || j.goal || "Nathan's choice";
  c.meta.textContent = `${j.manager || "…"} · ${ago(j.started)} ago`;
  c.head.setAttribute("aria-expanded", String(openId === j.id));
  const line = LIVE.has(j.status) && j.log.length ? j.log[j.log.length - 1] : "";
  c.last.textContent = line; c.last.hidden = !line;
  return c;
}

function note(s) {
  const lines = [];
  if (s.autopilot.on) lines.push(`Autopilot round ${s.autopilot.round}/${s.autopilot.rounds} · ${s.autopilot.applied} applied`);
  else if (s.autopilot.error) lines.push(`Autopilot stopped: ${s.autopilot.error}`);
  if (s.suggest_error && !s.suggesting) lines.push(`Ideas: ${s.suggest_error}`);
  $("ev-pilot-note").textContent = lines.join("  ·  ");
  if (wasSuggesting && !s.suggesting) {  // the ideas just arrived (or didn't)
    if (s.suggest_error) { toast(s.suggest_error, 6000); play("error"); } else { toast(`${s.ideas.length} ideas ready.`); play("click"); }
  }
  wasSuggesting = s.suggesting;
}

async function refresh() {
  // Polls and clicks can overlap. Each refresh takes a number; an older one that finishes
  // late stops instead of drawing over a newer one (that used to duplicate cards).
  const seq = ++refreshSeq;
  clearTimeout(timer);
  const s = await api("/api/evolve").catch(() => null);
  if (seq !== refreshSeq) return;
  if (!s) { schedule(false); return; }
  $("ev-source").textContent = s.ready ? `Source: ${s.source}` : "Nathan's source code wasn't found on this computer, so it can't change itself here.";
  for (const id of ["ev-go", "ev-auto", "ev-pilot"]) $(id).disabled = !s.ready || s.running || s.autopilot.on;
  $("ev-suggest").disabled = !s.ready || s.suggesting;
  $("ev-suggest").textContent = s.suggesting ? "Claude is reading the code…" : "✦ Ask Claude for ideas";
  $("ev-stop").hidden = !s.autopilot.on;
  note(s);
  $("ev-restart-row").hidden = !s.pending_restart;
  $("ev-restart").textContent = s.frozen ? "Rebuild & restart" : "Restart to load";
  $("ev-restart").disabled = !s.frozen;
  renderIdeas(s);

  const want = [];
  for (const j of s.jobs) {
    const c = cardFor(j);
    if (openId === j.id) { const node = await detailFor(c, j, seq); if (seq !== refreshSeq || !node) return; if (node.parentNode !== c.card) c.card.append(node); }
    else c.detail?.node.remove();
    want.push(c.card);
  }
  for (const id of cards.keys()) if (!s.jobs.some((j) => j.id === id)) cards.delete(id);
  const list = $("ev-jobs");
  if (!want.length) want.push(el("p", { class: "muted" }, "No improvements yet. Ask for one above, ask Claude for ideas, or let Nathan choose."));
  const have = [...list.children];
  // Re-attach only when the order changed: moving nodes would reset scroll positions.
  if (want.length !== have.length || want.some((n, i) => n !== have[i])) list.replaceChildren(...want);
  schedule(s.running || s.autopilot.on || s.suggesting);
}

function schedule(fast) {
  clearTimeout(timer);
  if (!$("view-evolve").hidden) timer = setTimeout(refresh, fast ? 2500 : 10000);
}

export function openEvolve() { wire(); refresh(); }
