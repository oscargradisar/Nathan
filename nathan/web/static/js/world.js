// World view: a rotating night-Earth fed by open data. Nathan's agent can fly it
// anywhere (world_view tool); you can too. The 3D globe loads on first visit.
import { $, api, el, toast } from "./core.js";
import { play } from "./sound.js";
import { GeoMediaLayer } from "./geomedia.js";

const LIB = "https://cdn.jsdelivr.net/npm/globe.gl@2.27.2/dist/globe.gl.min.js";
const IMG = "https://cdn.jsdelivr.net/npm/three-globe@2.31.1/example/img/";
const LAYERS = {
  planes: { color: "#3fd3ff", label: "Aircraft", size: 0.25 },
  quakes: { color: "#ff5f7e", label: "Earthquakes (24h)", size: 0.6 },
  iss: { color: "#ffb54a", label: "ISS", size: 1.2 },
  shops: { color: "#43e0a0", label: "Shops", size: 0.12 },
  media: { color: "#b59aff", label: "Media", size: 0.25 },
};
let world = null, on = new Set(["quakes", "iss"]), here = { lat: 46.05, lon: 14.51 }, data = {}, seenV = -1;
let media;

function loadLib() {
  return window.Globe ? Promise.resolve() : new Promise((ok, fail) => {
    const s = document.createElement("script"); s.src = LIB; s.onload = ok; s.onerror = fail; document.head.append(s);
  });
}

export async function openWorld() {
  if (!world) {
    try { await loadLib(); } catch { toast("The 3D globe needs an internet connection."); return; }
    const box = $("globe");
    world = window.Globe()(box)
      .globeImageUrl(IMG + "earth-night.jpg").bumpImageUrl(IMG + "earth-topology.png")
      .backgroundImageUrl(IMG + "night-sky.png").atmosphereColor("#3fd3ff").atmosphereAltitude(0.18)
      .pointLat("lat").pointLng("lon").pointColor((d) => LAYERS[d.layer].color)
      .pointAltitude((d) => d.layer === "iss" ? 0.06 : d.layer === "quakes" ? Math.max(0.01, d.mag / 40) : 0.01)
      .pointRadius((d) => LAYERS[d.layer].size * (d.layer === "quakes" ? Math.max(0.4, d.mag / 3) : 1))
      .pointLabel((d) => `<div class="g-tip"><b>${esc(d.label || "")}</b>${tip(d)}</div>`)
      .onPointClick((d) => readout(d))
      .ringLat("lat").ringLng("lon").ringColor(() => (t) => `rgba(63,211,255,${1 - t})`)
      .ringMaxRadius(4).ringPropagationSpeed(2).ringRepeatPeriod(1400)
      .width(box.clientWidth).height(box.clientHeight);
    world.controls().autoRotate = true; world.controls().autoRotateSpeed = 0.35;
    addEventListener("resize", () => !$("view-world").hidden && world.width($("globe").clientWidth).height($("globe").clientHeight));
    media = new GeoMediaLayer(world, drawCount);
    buildControls();
    // Both timers idle while the World view is hidden: no network or redraws in the background.
    setInterval(() => { if (!hidden()) followFocus(); }, 2500);
    setInterval(() => { if (!hidden() && on.has("iss")) refresh("iss"); }, 8000);
  } else {
    const box = $("globe");  // the window may have been resized while we were away
    world.width(box.clientWidth).height(box.clientHeight).resumeAnimation();
  }
  for (const l of on) refresh(l);
  followFocus();
}

// Leaving the World view: stop the 3D render loop, which would otherwise spin the GPU forever.
export function pauseWorld() { world?.pauseAnimation(); }
const hidden = () => document.hidden || $("view-world").hidden;

const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
function tip(d) {
  if (d.layer === "planes") return `<br>${esc(d.country || "")} · ${Math.round(d.alt_m || 0)} m · ${Math.round((d.speed_ms || 0) * 3.6)} km/h`;
  if (d.layer === "quakes") return `<br>magnitude ${d.mag}`;
  if (d.layer === "iss") return `<br>${d.alt_km} km up, 28,000 km/h`;
  return d.kind ? `<br>${esc(d.kind)}` : "";
}
function readout(d) { $("w-read").textContent = `${LAYERS[d.layer].label}: ${d.label || ""} (${d.lat.toFixed(2)}, ${d.lon.toFixed(2)})`; play("click"); }

async function refresh(layer) {
  if (layer === "media") { media.setVisible(true); drawCount(); return; }
  const q = layer === "planes" || layer === "shops" ? `?lat=${here.lat}&lon=${here.lon}` : "";
  const r = await api(`/api/globe/${layer}${q}`).catch(() => ({ items: [] }));
  if (r.error) toast(r.error);
  data[layer] = (r.items || []).map((d) => ({ ...d, layer }));
  draw();
}
function draw() {
  if (!world) return;
  const pts = [...on].flatMap((l) => data[l] || []);
  world.pointsData(pts).ringsData([...(on.has("iss") ? data.iss || [] : []), ...(on.has("quakes") ? data.quakes || [] : []).filter((q) => q.mag >= 4.5)]);
  drawCount();
}
function drawCount() {
  $("w-count").textContent = [...on].map((l) => l === "media" ? `${media?.count || 0} visible media` : `${(data[l] || []).length} ${LAYERS[l].label.toLowerCase()}`).join(" · ");
}

function buildControls() {
  $("w-form").querySelector("button").disabled = false;
  const box = $("w-layers"); box.textContent = "";
  for (const [id, l] of Object.entries(LAYERS)) {
    box.append(el("button", { class: "toggle", "aria-pressed": String(on.has(id)), style: `--c:${l.color}`,
      onclick: (e) => { on.has(id) ? on.delete(id) : on.add(id); e.currentTarget.setAttribute("aria-pressed", String(on.has(id)));
        if (id === "media") media.setVisible(on.has(id));
        on.has(id) ? refresh(id) : draw(); play("click"); } }, el("i", { class: "dot" }), l.label));
  }
  $("w-form").onsubmit = async (e) => {
    e.preventDefault();
    const prompt = $("w-place").value.trim(); if (!prompt) return;
    const button = e.currentTarget.querySelector("button"); button.disabled = true;
    $("w-read").textContent = "Finding cameras and globe data for " + prompt + "…";
    $("cctv-result").replaceChildren();

    try {
      const result = await api("/api/globe/search", { place: prompt });
      $("w-read").textContent = result.summary;
      await followFocus();
    } catch (error) { $("w-read").textContent = error.message; }
    finally { button.disabled = false; }
  };
}

async function followFocus() {
  const f = await api("/api/globe/focus").catch(() => null);
  if (!f || f.v === seenV || !world) return;
  const first = seenV === -1; seenV = f.v;
  here = { lat: f.lat, lon: f.lon };
  if (!first || f.v > 0) {
    world.controls().autoRotate = false;
    if (!f.media || f.media.id) world.pointOfView({ lat: f.lat, lng: f.lon, altitude: f.alt }, 2200);
    if (f.replaceLayers) on = new Set(f.layers);
    else f.layers.forEach((l) => on.add(l));
    if (f.media) {
      if (f.media.nodes) media.replace(f.media.nodes, f.media.id);
      f.media.visible ? on.add("media") : on.delete("media");
      media.filter({ category: f.media.category || "", region: f.media.region || "", country: f.media.country || "" });
    }
    media.setVisible(on.has("media")); buildControls();
    if (f.media?.id && f.media.visible) media.select(f.media.id, Boolean(f.media.nodes));
    if (f.label) { $("w-read").textContent = "Nathan is looking at " + f.label; play("route"); }
  }
  for (const l of on) refresh(l);
}
