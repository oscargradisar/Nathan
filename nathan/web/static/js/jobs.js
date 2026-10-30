// Jobs: separate pieces of work. Each keeps its own memory, files and history; the console
// works inside whichever job is picked (or none).
import { $, api, el, md, store, toast } from "./core.js";
import { play } from "./sound.js";

let wired = false, showArchived = false, openId = null;
const when = (t) => (t ? new Date(t * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "not used yet");

export const activeJob = () => store.get("job", null);

export function setActiveJob(job) {
  store.set("job", job ? { id: job.id, name: job.name } : null);
  paintPicker();
}

// ---- the console's job picker ------------------------------------------------------------
export async function paintPicker() {
  const pick = $("job-pick"); if (!pick) return;
  const { jobs } = await api("/api/jobs").catch(() => ({ jobs: [] }));
  const cur = activeJob();
  if (cur && !jobs.some((j) => j.id === cur.id)) store.set("job", null);  // archived elsewhere
  pick.replaceChildren(el("option", { value: "" }, "No job"),
    ...jobs.map((j) => el("option", { value: j.id, selected: activeJob()?.id === j.id }, j.name)),
    el("option", { value: "+new" }, "+ New job…"));
  pick.value = activeJob()?.id || "";
  pick.onchange = async () => {
    if (pick.value === "+new") { location.hash = "#jobs"; setTimeout(() => $("job-name").focus(), 50); pick.value = activeJob()?.id || ""; return; }
    setActiveJob(jobs.find((j) => j.id === pick.value) || null); play("click");
    toast(pick.value ? `Working inside “${activeJob().name}”. Its memory and files stay separate.` : "Not in a job.");
  };
}

// ---- the Jobs page -----------------------------------------------------------------------
function wire() {
  if (wired) return; wired = true;
  $("job-form").onsubmit = async (e) => {
    e.preventDefault();
    try {
      const job = await api("/api/jobs", { name: $("job-name").value, notes: $("job-notes").value });
      $("job-name").value = ""; $("job-notes").value = "";
      setActiveJob(job); play("done"); toast(`Job “${job.name}” created and picked in the console.`); render();
    } catch (err) { toast(err.message); }
  };
  $("job-archived").onclick = () => { showArchived = !showArchived; $("job-archived").setAttribute("aria-pressed", String(showArchived)); render(); };
}

async function history(job) {
  const { items } = await api(`/api/jobs/${job.id}/history`).catch(() => ({ items: [] }));
  if (!items.length) return el("p", { class: "muted" }, "Nothing asked in this job yet. Pick it in the console and ask away.");
  return el("div", { class: "job-history" }, ...items.slice(0, 30).map((h) =>
    el("details", {}, el("summary", {}, el("span", { class: "muted" }, when(h.ts)), " ", h.text.split("\n")[0].replace(/^Q: /, "").slice(0, 140)),
      (() => { const d = el("div", { class: "md" }); d.innerHTML = md(h.text.replace(/^Q: .*\n/, "").replace(/^A: /, "")); return d; })())));
}

async function card(job) {
  const active = activeJob()?.id === job.id;
  const notes = el("textarea", { rows: 3, "aria-label": `Notes for ${job.name}`, placeholder: "Notes Nathan should know for this job (course, deadlines, preferences…)" });
  notes.value = job.notes || "";
  const save = async (changes, msg) => { try { await api(`/api/jobs/${job.id}`, changes); if (msg) toast(msg); render(); } catch (e) { toast(e.message); } };
  const body = openId === job.id ? el("div", { class: "job-body" },
    notes, el("div", { class: "row" }, el("button", { class: "mini", onclick: () => save({ notes: notes.value }, "Notes saved.") }, "Save notes")),
    await history(job)) : null;
  return el("article", { class: "panel job" + (active ? " active" : "") },
    el("div", { class: "job-top" },
      el("button", { class: "job-name", "aria-expanded": String(openId === job.id), onclick: () => { openId = openId === job.id ? null : job.id; render(); } }, job.name),
      active ? el("span", { class: "chip live" }, "picked") : null,
      el("span", { class: "muted" }, `${job.asks} asks · ${when(job.last)}`)),
    job.notes && openId !== job.id ? el("div", { class: "muted job-notes" }, job.notes) : null,
    el("div", { class: "row" },
      job.archived ? null : el("button", { class: "go small", onclick: () => { setActiveJob(job); location.hash = "#console"; toast(`Working inside “${job.name}”.`); } }, "Work on this"),
      el("button", { class: "mini", onclick: () => api(`/api/jobs/${job.id}/folder`, {}).catch((e) => toast(e.message)) }, "Open files"),
      el("button", { class: "mini", onclick: () => { if (active) setActiveJob(null); save({ archived: !job.archived }, job.archived ? "Restored." : "Archived. Its memory stays, just out of the way."); } }, job.archived ? "Restore" : "Archive")),
    body);
}

async function render() {
  const { jobs } = await api(`/api/jobs?archived=${showArchived}`).catch(() => ({ jobs: [] }));
  const list = $("job-list"); list.replaceChildren();
  if (!jobs.length) list.append(el("p", { class: "muted" }, showArchived ? "No archived jobs." : "No jobs yet. Make one above: a class, a project, a trip…"));
  for (const j of jobs.sort((a, b) => (b.last || b.created) - (a.last || a.created))) list.append(await card(j));
  paintPicker();
}

export function openJobs() { wire(); render(); }
