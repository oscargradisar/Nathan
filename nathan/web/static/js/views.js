// Staff, Accounts, History and Systems views.
import { $, api, el, follow, provider, short, toast } from "./core.js";
import { play } from "./sound.js";

const RANKS = ["Champion", "Diamond", "Platinum", "Gold", "Silver", "Bronze", "Iron"];

// ---- staff -------------------------------------------------------------------------------
export function renderStaff(staff) {
  const counts = Object.fromEntries(RANKS.map((r) => [r, 0]));
  staff.forEach((s) => counts[s.rank]++);
  $("ranks").textContent = "";
  RANKS.forEach((r) => $("ranks").append(el("span", { class: "rank rank-" + r.toLowerCase() }, `${r} ${counts[r]}`)));
  const body = $("board"); body.textContent = "";
  for (const s of staff) {
    body.append(el("tr", { class: s.working ? "busy" : "" },
      el("td", {}, s.place ?? "–"),
      el("td", {}, el("span", { class: "rank rank-" + s.rank.toLowerCase() }, s.rank)),
      el("td", {}, short(s.ref), el("span", { class: "sub" }, ` · ${provider(s.ref)}${s.working ? " · working now" : ""}`)),
      el("td", { class: "num" }, s.elo), el("td", { class: "num" }, s.priority ?? s.elo),
      el("td", { class: "num" }, s.done), el("td", { class: "num" }, s.no_shows),
      el("td", { class: "num" }, `${s.wins}–${s.losses}`), el("td", { class: "num" }, s.avg_score ?? "–"),
      el("td", { class: "num" }, s.avg_seconds != null ? s.avg_seconds + "s" : "–"),
      el("td", { class: "td-note" }, s.verdict ? `${s.verdict.by}: ${s.verdict.score}/10 · ${s.verdict.note}` : "")));
  }
  const duty = $("duty"); duty.textContent = "";
  const sorted = [...staff.filter((s) => s.working), ...staff.filter((s) => !s.working)].slice(0, 14);
  for (const s of sorted) duty.append(el("div", { class: "d" + (s.working ? " busy" : "") }, el("i"), el("span", {}, short(s.ref)), el("span", {}, `${s.rank} ${s.elo}`)));
}

// ---- accounts ----------------------------------------------------------------------------
const STATE = { connected: "connected", waiting: "waiting for you", failed: "failed", "not connected": "not connected" };
let accounts = [];

export async function renderAccounts() {
  accounts = (await api("/api/connect")).providers;
  const box = $("accounts"); box.textContent = "";
  for (const p of accounts) box.append(accountCard(p));
  box.append(await canvasCard());
  return accounts;
}

// Canvas (school): an address + personal access token, checked against Canvas, kept in the keychain.
async function canvasCard() {
  const st = await api("/api/canvas").catch(() => ({ connected: false }));
  const url = el("input", { type: "url", placeholder: "https://yourschool.instructure.com", value: st.url || "", "aria-label": "Canvas address" });
  const token = el("input", { type: "password", placeholder: "access token", "aria-label": "Canvas access token" });
  const save = el("button", { class: "mini", onclick: async () => {
    try { const r = await api("/api/canvas", { url: url.value, token: token.value }); play("done"); toast(`Canvas connected as ${r.name}.`); renderAccounts(); }
    catch (e) { play("fail"); toast(e.message, 6000); }
  } }, st.connected ? "Replace" : "Connect");
  const state = st.connected ? "connected" : "not connected";
  return el("div", { class: "panel acct" },
    el("div", { class: "a-top" }, el("span", { class: "a-name" }, "canvas"), el("span", { class: "state " + state.replace(" ", "-") }, STATE[state] || state)),
    el("div", { class: "a-info" }, "school assignments · Canvas → Account → Settings → + New Access Token, then paste it here with your school's Canvas address"),
    el("div", { class: "paste open" }, url, token, save));
}

function accountCard(p) {
  const free = p.method === "none";
  const how = free ? "free gateway · no account needed" : p.method === "oauth_pkce" ? "sign in with OpenRouter"
    : p.method === "extension" ? "copy the key and it arrives here" : "paste key";
  const cap = p.daily_requests ? `${p.daily_requests} requests/day` : `${p.models} models`;
  const input = el("input", { type: "password", placeholder: "paste the key", "aria-label": `${p.provider} key` });
  const paste = el("div", { class: "paste" }, input, el("button", { class: "mini", onclick: async () => {
    try { await api("/api/keys/manual", { provider: p.provider, key: input.value.trim() }); play("done"); toast(`${p.provider} connected.`); }
    catch (e) { play("fail"); toast(e.message, 4500); }
    renderAccounts();
  } }, "Save"));
  const state = free ? "connected" : p.state;
  const actions = free ? null : el("div", { class: "a-actions" },
    state !== "connected" && el("button", { class: "go small", onclick: () => connect(p) }, "Get key"),
    el("button", { class: "mini", onclick: () => paste.classList.toggle("open") }, state === "connected" ? "Replace key" : "Paste key"),
    state === "connected" && el("button", { class: "mini", onclick: async () => {
      await api(`/api/disconnect/${p.provider}`, {});
      if (confirm(`Removed from this computer. Open ${p.provider}'s page to revoke the key there too?`)) api(`/api/key-page/${p.provider}`, {});
      renderAccounts();
    } }, "Disconnect"));
  return el("div", { class: "panel acct" },
    el("div", { class: "a-top" }, el("span", { class: "a-name" }, p.provider), el("span", { class: "state " + state.replace(" ", "-") }, STATE[state] || state)),
    el("div", { class: "a-info" }, `${how} · adds ${cap}` + (p.detail ? ` · ${p.detail}` : "") + (p.note ? ` · ${p.note}` : "")),
    actions, paste);
}

async function connect(p) {
  try {
    await api(`/api/connect/${p.provider}`, {});
    play("route");
    toast(p.method === "manual" ? "Key page opened. Create a key and paste it here." : "Key page opened in your browser. Create a key and press Copy.", 4500);
    if (p.method === "manual") renderAccounts().then(() => document.querySelector(`[aria-label="${p.provider} key"]`)?.parentElement.classList.add("open"));
  } catch (e) { toast(e.message, 4500); }
}

// ---- premium plans (managers) ------------------------------------------------------------
const ROLE_NOTE = {
  claude: "the Overlord: plans, picks workers and writes in Overlord mode; final word on reviews",
  chatgpt: "assistant manager · runs Programming · searches the web",
};

export async function renderManagers() {
  const { managers } = await api("/api/managers");
  const box = $("managers"); box.textContent = "";
  for (const m of managers) {
    const state = m.signed_in ? "connected" : m.installed ? "not signed in" : "not installed";
    const busy = el("span", { class: "muted" });
    const act = async (path, msg) => {
      busy.textContent = msg;
      try { await api(path, {}); play("route"); } catch (e) { toast(e.message, 6000); play("fail"); }
      busy.textContent = ""; renderManagers();
    };
    box.append(el("div", { class: "panel acct mgr" },
      el("div", { class: "a-top" }, el("span", { class: "a-name" }, m.name), el("span", { class: "title" }, m.title),
        el("span", { class: "state " + (m.signed_in ? "connected" : "waiting") }, state)),
      el("div", { class: "a-info" }, `${ROLE_NOTE[m.id]} · uses your ${m.plan} plan`),
      el("div", { class: "a-actions" },
        !m.installed && el("button", { class: "go small", onclick: () => act(`/api/managers/${m.id}/install`, "installing (a minute or two)…") }, "Install"),
        m.installed && !m.signed_in && el("button", { class: "go small", onclick: async () => {
          await act(`/api/managers/${m.id}/signin`, "");
          toast("Your browser opened to sign in. Come back when it says you're done.", 7000);
          const until = Date.now() + 5 * 60 * 1000;
          const poll = setInterval(async () => {
            const s = (await api("/api/managers")).managers.find((x) => x.id === m.id);
            if (s?.signed_in || Date.now() > until) { clearInterval(poll); renderManagers(); if (s?.signed_in) { play("final"); toast(`${m.name} is on the management team.`); } }
          }, 4000);
        } }, "Sign in"),
        el("button", { class: "mini", onclick: renderManagers }, "Refresh"), busy)));
  }
  return managers;
}

// ---- programming projects ----------------------------------------------------------------
export async function renderProjects() {
  const { projects } = await api("/api/projects");
  const list = $("projects"); list.textContent = "";
  if (!projects.length) list.append(el("li", {}, el("span", { class: "h-q muted" }, "No projects yet. The first one is always the hardest; the models won't tell you that.")));
  for (const p of projects) {
    list.append(el("li", { tabindex: "0", onclick: () => api(`/api/projects/${encodeURIComponent(p.name)}/open`, {}) },
      el("time", {}, new Date(p.ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" })),
      el("span", { class: "h-q" }, p.name), el("span", { class: "h-mode" }, `${p.files} file${p.files === 1 ? "" : "s"} · open folder`)));
  }
}

// ---- history -----------------------------------------------------------------------------
export async function renderPrayers() {
  const { prayers } = await api("/api/prayers");
  const list = $("prayers"); list.textContent = "";
  if (!prayers.length) list.append(el("li", {}, el("span", { class: "h-q muted" }, "No prayers yet. The team only prays when it's genuinely unsure, which is more self-awareness than most teams.")));
  for (const p of prayers) {
    const d = new Date(p.ts * 1000);
    list.append(el("li", { class: "prayer-item", title: p.prompt ? "Asked during: " + p.prompt : "" },
      el("time", {}, d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " " + d.toTimeString().slice(0, 5)),
      el("span", { class: "h-q" }, p.text),
      el("span", { class: "h-mode" + (p.answered ? " ok" : "") }, p.answered ? "answered" : "unanswered")));
  }
}

export async function renderHistory(onPick) {
  const items = (await api("/api/history?limit=60")).items;
  const list = $("history"); list.textContent = "";
  if (!items.length) list.append(el("li", {}, el("span", { class: "h-q muted" }, "Nothing yet. History begins with your first question, as history does.")));
  for (const it of items) {
    const d = new Date(it.ts * 1000);
    list.append(el("li", { tabindex: "0", onclick: () => onPick(it.question),
      onkeydown: (e) => { if (e.key === "Enter") onPick(it.question); } },
    el("time", {}, d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " " + d.toTimeString().slice(0, 5)),
    el("span", { class: "h-q" }, it.question), el("span", { class: "h-mode" }, it.mode)));
  }
}

// ---- systems -----------------------------------------------------------------------------
export function renderSystems(status) {
  const box = $("sys-info"); box.textContent = "";
  const rc = status.roll_call;
  const items = [
    [status.version, "version"], [status.accounts.length, "accounts connected"],
    [status.gateways.length, "free gateways"], [`${status.staff_ready}/${status.staff_total}`, "models ready"],
    [status.memories, "memories"],
    [rc ? (rc.error ? "failed" : `${rc.retired.length} retired · ${rc.reinstated.length} back`) : "checking…", "startup roll call"],
    [status.cache_hits, "answers from cache (free)"],
    [status.last_check ? `${status.last_check.ok}/${status.last_check.total}` : "never", "last nightly check"],
  ];
  for (const [v, k] of items) box.append(el("div", { class: "panel" }, el("b", {}, String(v)), el("span", {}, k)));
}

export async function initSystemsControls() {
  const btn = $("nightly"), note = $("nightly-note");
  const paint = (s) => {
    btn.setAttribute("aria-pressed", String(!!s.on));
    btn.disabled = !s.supported;
    note.textContent = !s.supported ? "needs Windows or macOS" : s.on ? `on · runs at ${s.at}, pings every model once` : "off";
  };
  let state = await api("/api/schedule").catch(() => ({ supported: false }));
  paint(state);
  btn.onclick = async () => {
    try { state = { ...state, ...(await api("/api/schedule", { on: !state.on })) }; paint(state); play("click"); toast(`Nightly check ${state.on ? "on" : "off"}.`); }
    catch (e) { toast(e.message, 5000); }
  };
  $("open-ws").onclick = () => api("/api/open-workspace", {}).catch((e) => toast(e.message));
}

export async function runDiagnostics(done) {
  const btn = $("diag"); btn.disabled = true;
  $("diag-note").textContent = "pinging every model once…";
  const grid = $("diag-units"); grid.textContent = "";
  $("diag-report").hidden = true;
  const cards = new Map();
  play("start");
  let run;
  try { run = await api("/api/runs", { prompt: "systems check", mode: "diagnostics" }); }
  catch (e) { toast(e.message); btn.disabled = false; return; }
  follow(run.id, 0, (ev) => {
    if (ev.role === "patient" && ev.type === "working") {
      const c = el("div", { class: "unit working" }, el("div", { class: "u-top" }, el("span", { class: "u-role" }, provider(ev.id)), el("span", { class: "u-state" }, "pinging")),
        el("div", { class: "u-model" }, short(ev.id)));
      cards.set(ev.id, c); grid.append(c);
    } else if (ev.role === "patient" && (ev.type === "delivered" || ev.type === "task_failed")) {
      const c = cards.get(ev.id); if (!c) return;
      const ok = ev.type === "delivered";
      c.className = "unit " + (ok ? "done" : "failed");
      c.querySelector(".u-state").textContent = ok ? `${ev.seconds}s` : ev.reason;
      play(ok ? "work" : "fail");
    } else if (ev.type === "final") {
      $("diag-report").hidden = false; $("diag-report").textContent = ev.text; play("final");
    } else if (ev.type === "done") {
      btn.disabled = false; $("diag-note").textContent = "done"; done && done();
    }
  });
}
