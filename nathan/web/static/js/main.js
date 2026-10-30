// Nathan console entry point: views, status chips, toggles, keyboard shortcuts.
import { $, api, el, store, toast } from "./core.js";
import { ask, initConsole, log, onRunFinished, setStaff } from "./console.js";
import { play, setSound, soundOn } from "./sound.js";
import { initSystemsControls, renderAccounts, renderHistory, renderManagers, renderPrayers, renderProjects,
  renderStaff, renderSystems, runDiagnostics } from "./views.js";
import { setVoice, voiceOn } from "./voice.js";
import { lightningOn, setLightning, strike } from "./fx.js";
import { initMorphy } from "./morphy.js";
import { boot as bootScreen } from "./boot.js";
import { openWorld } from "./world.js";

const VIEWS = ["console", "world", "code", "staff", "accounts", "history", "systems"];
let status = null;
let worldVersion = null;

async function followWorldRequest() {
  const focus = await api("/api/globe/focus").catch(() => null);
  if (!focus) return;
  const changed = worldVersion !== null && worldVersion !== focus.v;
  worldVersion = focus.v;
  if (changed && focus.media?.nodes?.length) location.hash = "#world";
}

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
  if (name === "world") openWorld();
  if (name === "history") { renderPrayers(); renderHistory((q) => ask(q)); }
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
  toggle($("t-bolt"), lightningOn, (on) => { setLightning(on); if (on) setTimeout(() => strike($("core"), { big: true }), 50); }, "Lightning");
  initMorphy();
  initLead(); initMenu();
  await refreshStatus().catch(() => null);
  const booting = bootScreen(status);
  await refreshStaff().catch(() => null);
  await booting;
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
  followWorldRequest();
  setInterval(() => { refreshStatus().catch(() => null); refreshStaff().catch(() => null); followWorldRequest(); }, 5000);

  document.addEventListener("keydown", (e) => {
    const typing = /INPUT|TEXTAREA/.test(document.activeElement?.tagName || "");
    if (e.key === "/" && !typing) { e.preventDefault(); location.hash = "#console"; $("prompt").focus(); }
    if (e.altKey && /^[1-7]$/.test(e.key)) { e.preventDefault(); location.hash = "#" + VIEWS[Number(e.key) - 1]; }
  });
  if (store.get("seen-tips", false) === false) {
    log("Tips: / focuses the command bar · Alt+1…6 switches sections · Shift+Enter adds a line.", "sys");
    store.set("seen-tips", true);
  }
}
function initMenu() {
  const btn = $("menu-btn"), panel = $("menu");
  const open = (on) => { panel.hidden = !on; btn.setAttribute("aria-expanded", String(on)); if (on) play("click"); };
  btn.onclick = (e) => { e.stopPropagation(); open(panel.hidden); };
  panel.onclick = (e) => { if (e.target.closest("a")) open(false); };
  document.addEventListener("click", (e) => { if (!panel.hidden && !panel.contains(e.target)) open(false); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !panel.hidden) { open(false); btn.focus(); }
    if (e.ctrlKey && e.key === ",") { e.preventDefault(); open(panel.hidden); }
    if (e.ctrlKey && e.key.toLowerCase() === "q") { e.preventDefault(); quit(); }
  });
  $("quit").onclick = quit;
}

async function quit() {
  const running = (await api("/api/status").catch(() => ({}))).active_runs || 0;
  if (running && !confirm(`${running} task${running > 1 ? "s are" : " is"} still running and will stop. Exit anyway?`)) return;
  await api("/api/quit", {}).catch(() => null);
  $("bye").hidden = false;
  window.close();  // works in Nathan's own window; elsewhere the goodbye screen stays
}

async function initLead() {
  const paint = (m) => { for (const b of $("lead").querySelectorAll("button")) b.setAttribute("aria-checked", String(b.dataset.m === m)); };
  const s = await api("/api/settings").catch(() => ({ manager: "chatgpt" }));
  paint(s.manager);
  for (const b of $("lead").querySelectorAll("button")) b.onclick = async () => {
    try { paint((await api("/api/settings", { manager: b.dataset.m })).manager); play("click");
      toast(`${b.textContent} is managing. ${b.dataset.m === "claude" ? "Sharper, and pricier on your plan." : "Cheaper, still competent."}`); }
    catch (e) { toast(e.message); }
  };
}

async function codeBoss() {
  const { managers } = await api("/api/managers").catch(() => ({ managers: [] }));
  const lead = (await api("/api/settings").catch(() => ({ manager: "chatgpt" }))).manager;
  const order = [lead, lead === "chatgpt" ? "claude" : "chatgpt"];
  const boss = order.map((id) => managers.find((m) => m.id === id && m.signed_in)).find(Boolean);
  $("code-boss").textContent = boss ? boss.name : "The free team's planner";
  $("code-note").textContent = boss ? `${boss.name} is managing.` : "Connect ChatGPT under Accounts to put it in charge.";
}

boot();
