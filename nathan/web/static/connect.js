"use strict";
const $ = (id) => document.getElementById(id);
const post = (path, body = {}) => fetch(path, {
  method: "POST", headers: { "content-type": "application/json", "x-nathan": "1" }, body: JSON.stringify(body),
}).then(async (r) => { const j = await r.json().catch(() => ({})); if (!r.ok) throw new Error(j.detail || r.status); return j; });

const LABEL = { "not connected": "not connected", waiting: "waiting for you", connected: "connected", failed: "failed" };
const CLS = { connected: "ok", failed: "bad", waiting: "run" };

function row(p) {
  const div = document.createElement("div");
  div.className = "card"; div.style.marginBottom = "10px"; div.id = "p-" + p.provider;
  const cap = p.daily_requests ? `${p.daily_requests} requests/day` : `${p.models} models`;
  div.innerHTML = `
    <div class="prov">
      <div><strong></strong> <span class="badge"></span>
        <div class="muted info"></div></div>
      <div class="actions">
        <button class="go primary">Get key</button>
        <button class="pastebtn">Paste key</button>
        <button class="off">Disconnect</button>
      </div>
    </div>
    <div class="paste"><input type="password" placeholder="paste the key"><button class="save">Save</button></div>`;
  div.querySelector("strong").textContent = p.provider;
  const badge = div.querySelector(".badge");
  badge.textContent = LABEL[p.state] || p.state; badge.className = "badge " + (CLS[p.state] || "");
  const extra = p.missing_env.length ? ` · needs ${p.missing_env.join(", ")} in .env` : "";
  div.querySelector(".info").textContent =
    `${p.method === "oauth_pkce" ? "sign in with OpenRouter" : p.method === "extension" ? "copy the key and it arrives here" : "paste key"} · adds ${cap}${extra}` +
    (p.detail ? ` · ${p.detail}` : "") + (p.note ? ` · ${p.note}` : "");
  div.querySelector(".off").hidden = p.state !== "connected";
  if (p.method === "none") {  // free gateway: nothing to connect or remove
    div.querySelector(".actions").hidden = true;
    div.querySelector(".info").textContent = `free, no account needed · adds ${cap}`;
  }
  div.querySelector(".go").onclick = () => connect(p.provider);
  div.querySelector(".pastebtn").onclick = () => div.querySelector(".paste").classList.toggle("open");
  div.querySelector(".save").onclick = async () => {
    const key = div.querySelector(".paste input").value.trim();
    try { await post("/api/keys/manual", { provider: p.provider, key }); } catch (e) { alert(e.message); }
    refresh();
  };
  div.querySelector(".off").onclick = async () => {
    const r = await post(`/api/disconnect/${p.provider}`);
    if (r.revoke_at && confirm("Removed from this computer. Open the provider page to revoke the key too?")) post(`/api/key-page/${p.provider}`);
    refresh();
  };
  return div;
}

let providers = [];
async function refresh() {
  providers = (await fetch("/api/connect").then((r) => r.json())).providers;
  const list = $("list"); list.textContent = "";
  providers.forEach((p) => list.append(row(p)));
  const n = providers.filter((p) => p.state === "connected").length;
  $("summary").textContent = `${n} of ${providers.length} connected`;
  if (n >= 2 && new URLSearchParams(location.search).has("first")) location.href = "/";
}

async function connect(name) {
  try {
    const r = await post(`/api/connect/${name}`);
    if (r.method === "manual") document.querySelector(`#p-${name} .paste`).classList.add("open");
  } catch (e) { alert(e.message); }
  refresh();
}

async function waitConnected(name, ms) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    await new Promise((r) => setTimeout(r, 2000));
    await refresh();
    const p = providers.find((x) => x.provider === name);
    if (p && (p.state === "connected" || p.state === "failed")) return p.state;
  }
  return "timeout";
}

$("all").onclick = async () => {
  for (const p of providers.filter((x) => x.state !== "connected" && x.method !== "manual")) {
    await connect(p.provider);
    $("summary").textContent = `connecting ${p.provider}… finish in the new tab`;
    await waitConnected(p.provider, 5 * 60 * 1000);
  }
  refresh();
};
refresh();
setInterval(refresh, 5000);
