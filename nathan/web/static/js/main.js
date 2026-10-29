// Nathan console entry point: views, status chips, toggles, keyboard shortcuts.
import { $, api, el, store, toast } from "./core.js";
import { ask, initConsole, log, onRunFinished, setStaff } from "./console.js";
import { play, setSound, soundOn } from "./sound.js";
import { initSystemsControls, renderAccounts, renderHistory, renderManagers, renderProjects, renderStaff,
  renderSystems, runDiagnostics } from "./views.js";
import { setVoice, voiceOn } from "./voice.js";

const VIEWS = ["console", "code", "staff", "accounts", "history", "systems"];
let status = null;

// Old page addresses (/connect, /workflow, /leaderboard) land on the matching view.
const legacy = { "/connect": "accounts", "/leaderboard": "staff", "/workflow": "console" }[location.pathname];
if (legacy && !location.hash) history.replaceState(null, "", "/#" + legacy + location.search);

function show() {
  const name = VIEWS.includes(location.hash.slice(1)) ? location.hash.slice(1) : "console";
  for (const v of VIEWS) $("view-" + v).hidden = v !== name;
  for (const a of document.querySelectorAll(".rail a")) a.classList.toggle("on", a.dataset.view === name);
  if (name === "staff") refreshStaff();
  if (name === "accounts") { renderManagers(); renderAccounts(); }
  if (name === "code") { renderProjects(); codeBoss(); }
  if (name === "history") renderHistory((q) => ask(q));
  if (name === "systems") refreshStatus().then(() => renderSystems(status));
  if (name === "console") setTimeout(() => $("prompt").focus(), 0);
}
window.addEventListener("hashchange", () => { play("click"); show(); });

async function refreshStatus() {
  status = await api("/api/status");
  const c = $("chips"); c.textContent = "";
  c.append(el("span", { class: "chip" }, el("b", {}, String(status.staff_ready)), ` / ${status.staff_total} models ready`));
  c.append(el("span", { class: "chip" }, el("b", {}, String(status.accounts.length)), " accounts"));
  if (status.active_runs) c.append(el("span", { class: "chip live" }, `${status.active_runs} running`));
  return status;
}

async function refreshStaff() {
  const staff = (await api("/api/staff")).staff;
  setStaff(staff); renderStaff(staff);
}

function toggle(btn, get, set, name) {
  const paint = () => btn.setAttribute("aria-pressed", String(get()));
  btn.onclick = () => { set(!get()); paint(); play("click"); toast(`${name} ${get() ? "on" : "off"}.`); };
  paint();
}

async function boot() {
  toggle($("t-sound"), soundOn, setSound, "Sound");
  toggle($("t-voice"), voiceOn, setVoice, "Voice");
  await refreshStatus().catch(() => null);
  await refreshStaff().catch(() => null);
  initConsole(status ? status.staff_ready : "several");
  onRunFinished(() => { refreshStaff(); refreshStatus(); });
  initSystemsControls();
  $("code-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const text = $("code-prompt").value.trim();
    if (!text) return;
    $("code-prompt").value = "";
    ask(text, "code");
  });
  $("code-prompt").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) $("code-form").requestSubmit();
  });
  $("review-btn").onclick = () => ask("Performance review of the staff", "review");
  $("diag").onclick = () => runDiagnostics(() => { refreshStaff(); refreshStatus().then(() => renderSystems(status)); });

  // First run: send people to Accounts until two are connected, then come back by itself.
  if (status && status.accounts.length < 2) {
    $("first-run").hidden = false;
    if (!location.hash || location.hash === "#console") location.hash = "#accounts";
    const wait = setInterval(async () => {
      const s = await refreshStatus().catch(() => null);
      if (location.hash === "#accounts") renderAccounts();
      if (s && s.accounts.length >= 2) {
        clearInterval(wait); $("first-run").hidden = true;
        log("Two accounts connected. The company is open for business.", "good"); play("final");
        location.hash = "#console";
      }
    }, 4000);
  }
  show();

  // Live chips and staff; this polling also tells the desktop app the window is still open.
  setInterval(() => { refreshStatus().catch(() => null); refreshStaff().catch(() => null); }, 5000);

  document.addEventListener("keydown", (e) => {
    const typing = /INPUT|TEXTAREA/.test(document.activeElement?.tagName || "");
    if (e.key === "/" && !typing) { e.preventDefault(); location.hash = "#console"; $("prompt").focus(); }
    if (e.altKey && /^[1-6]$/.test(e.key)) { e.preventDefault(); location.hash = "#" + VIEWS[Number(e.key) - 1]; }
  });
  if (store.get("seen-tips", false) === false) {
    log("Tips: / focuses the command bar · Alt+1…6 switches sections · Shift+Enter adds a line.", "sys");
    store.set("seen-tips", true);
  }
}
async function codeBoss() {
  const { managers } = await api("/api/managers").catch(() => ({ managers: [] }));
  const order = ["chatgpt", "claude", "gemini"];
  const boss = order.map((id) => managers.find((m) => m.id === id && m.signed_in)).find(Boolean);
  $("code-boss").textContent = boss ? boss.name : "The free team's planner";
  $("code-note").textContent = boss ? `${boss.name} is managing.` : "Connect ChatGPT under Accounts to put it in charge.";
}

boot();
