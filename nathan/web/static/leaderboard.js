"use strict";
const $ = (id) => document.getElementById(id);
const RANKS = ["Champion", "Diamond", "Platinum", "Gold", "Silver", "Bronze", "Iron"];

function td(text, cls = "") { const c = document.createElement("td"); c.textContent = text; if (cls) c.className = cls; return c; }

async function load() {
  const staff = (await fetch("/api/staff").then((r) => r.json())).staff;
  const counts = Object.fromEntries(RANKS.map((r) => [r, 0]));
  staff.forEach((s) => counts[s.rank]++);
  $("ranks").textContent = "";
  for (const r of RANKS) {
    const chip = document.createElement("span");
    chip.className = "rank rank-" + r.toLowerCase(); chip.textContent = `${r} ${counts[r]}`;
    $("ranks").append(chip);
  }
  const body = $("board"); body.textContent = "";
  for (const s of staff) {
    const tr = document.createElement("tr");
    if (s.working) tr.className = "busy";
    const rank = td(""); const chip = document.createElement("span");
    chip.className = "rank rank-" + s.rank.toLowerCase(); chip.textContent = s.rank; rank.append(chip);
    const [provider, ...model] = s.ref.split("/");
    const name = td(""); name.append(model.join("/"), Object.assign(document.createElement("span"), { className: "muted", textContent: ` · ${provider}${s.working ? " · working now" : ""}` }));
    tr.append(td(s.place ?? "–"), rank, name, td(s.elo, "num"), td(s.done, "num"), td(s.no_shows, "num"),
      td(`${s.wins}–${s.losses}`, "num"), td(s.avg_score ?? "–", "num"), td(s.avg_seconds != null ? s.avg_seconds + "s" : "–", "num"));
    body.append(tr);
  }
}
load();
setInterval(load, 5000);  // live; also tells the desktop app the window is still open
