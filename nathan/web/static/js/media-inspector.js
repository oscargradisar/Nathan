import { $, el } from "./core.js";

let hlsLibrary;
function loadHls() {
  if (window.Hls) return Promise.resolve(window.Hls);
  if (!hlsLibrary) hlsLibrary = new Promise((resolve, reject) => {
    const script = el("script", { src: "/static/vendor/hls.min.js" });
    script.onload = () => resolve(window.Hls);
    script.onerror = () => { script.remove(); hlsLibrary = null; reject(new Error("HLS unavailable")); };
    document.head.append(script);
  });
  return hlsLibrary;
}

export function frameUrl(url, timestamp = Date.now()) {
  const result = new URL(url, globalThis.location?.href || "http://localhost/");
  result.searchParams.set("_geomedia", String(timestamp));
  return result.href;
}

export class MediaInspectorModal {
  constructor() {
    this.panel = $("media-inspector"); this.generation = 0;
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !this.panel.hidden) this.close(); });
    new MutationObserver(() => { if ($("view-world").hidden) this.close(false); })
      .observe($("view-world"), { attributes: true, attributeFilter: ["hidden"] });
  }
  close(restoreFocus = true) {
    this.generation++; clearTimeout(this.timer); clearTimeout(this.timeout);
    if (this.preload) { this.preload.onload = this.preload.onerror = null; this.preload.src = ""; this.preload = null; }
    this.hls?.destroy(); this.hls = null;
    if (this.video) { this.video.pause(); this.video.removeAttribute("src"); this.video.load(); this.video = null; }
    this.panel.replaceChildren(); this.panel.hidden = true;
    if (restoreFocus && this.previousFocus?.isConnected) this.previousFocus.focus();
  }
  async open(node, onFailure) {
    this.close(false); this.previousFocus = document.activeElement;
    const generation = this.generation, alive = () => generation === this.generation;
    this.panel.hidden = false; this.paused = false;
    const close = el("button", { class: "toggle", onclick: () => this.close(), "aria-label": "Close media inspector" }, "×");
    const status = el("div", { class: "media-skeleton", role: "status" }, "Loading media…");
    const stage = el("div", { class: "media-stage" });
    const direct = el("a", { href: node.streamUrl, target: "_blank", rel: "noopener noreferrer", class: "toggle" }, "Open source ↗");
    const pause = el("button", { class: "toggle", onclick: () => toggle() }, "Pause");
    const refresh = el("button", { class: "toggle", onclick: () => this.open(node) }, "Refresh");
    this.panel.append(el("header", {}, el("h2", { id: "media-title" }, node.title), close),
      el("div", { class: "media-meta" }, `${node.category} · ${[node.region, node.country].filter(Boolean).join(", ")}`),
      el("code", { class: "media-coordinates" }, `${node.coordinates[1].toFixed(5)}, ${node.coordinates[0].toFixed(5)}`),
      status, stage, el("div", { class: "row media-actions" }, pause, refresh, direct));
    if (node.attributionUrl) this.panel.append(el("a", { href: node.attributionUrl, target: "_blank", rel: "noopener noreferrer" }, "Source attribution ↗"));
    if (node.streamType === "iframe") this.panel.append(el("p", { class: "muted" }, "If the provider blocks embedding, use Open source. Playback controls are inside the embed."));
    close.focus();
    const loaded = () => { if (alive()) { clearTimeout(this.timeout); status.hidden = true; } };
    const failed = () => {
      if (!alive()) return;
      clearTimeout(this.timeout); clearTimeout(this.timer);
      this.hls?.destroy(); this.hls = null;
      status.hidden = false; status.className = "media-error";
      status.textContent = "Stream Offline / CORS Blocked — try Refresh or Open source.";
      const retry = onFailure; onFailure = null; retry?.();
    };
    const startTimeout = () => { clearTimeout(this.timeout); this.timeout = setTimeout(failed, 20000); };
    let poll = () => {};
    const toggle = () => {
      this.paused = !this.paused; pause.textContent = this.paused ? "Play" : "Pause";
      if (this.video) {
        if (this.paused) this.video.pause(); else this.video.play().catch(() => { pause.textContent = "Play"; });
      } else if (node.streamType === "image-poll") {
        clearTimeout(this.timer); if (!this.paused) poll();
      } else {
        const frame = stage.querySelector("iframe");
        if (this.paused) { frame.src = "about:blank"; clearTimeout(this.timeout); }
        else { status.hidden = false; startTimeout(); frame.src = node.streamUrl; }
      }
    };
    startTimeout();
    if (node.streamType === "image-poll") {
      const image = el("img", { alt: node.title }); stage.append(image);
      poll = () => {
        if (!alive() || this.paused || this.preload) return;
        const next = this.preload = new Image(); startTimeout();
        next.onload = () => {
          if (!alive()) return;
          this.preload = null;
          if (!this.paused) { image.src = next.src; loaded(); }
          if (!this.paused) this.timer = setTimeout(poll, node.refreshIntervalMs || 10000);
        };
        next.onerror = () => { this.preload = null; failed(); };
        next.src = frameUrl(node.streamUrl);
      };
      poll();
    } else if (node.streamType === "iframe") {
      stage.append(el("iframe", { src: node.streamUrl, title: node.title,
        sandbox: "allow-scripts allow-presentation", allow: "fullscreen", referrerpolicy: "no-referrer",
        onload: loaded, onerror: failed }));
    } else {
      const video = this.video = el("video", { controls: true, playsinline: true, preload: "metadata",
        onloadedmetadata: loaded, onloadeddata: loaded, onerror: failed,
        onplay: () => { this.paused = false; pause.textContent = "Pause"; },
        onpause: () => { this.paused = true; pause.textContent = "Play"; } });
      // Muted so the browser allows it to start on its own; the controls can unmute.
      video.muted = true; video.autoplay = true; stage.append(video);
      if (node.streamType === "mp4" || video.canPlayType("application/vnd.apple.mpegurl")) video.src = node.streamUrl;
      else {
        try {
          const Hls = await loadHls(); if (!alive()) return;
          if (!Hls.isSupported()) { failed(); return; }
          this.hls = new Hls();
          this.hls.on(Hls.Events.ERROR, (_, data) => { if (data.fatal) failed(); });
          this.hls.loadSource(node.streamUrl); this.hls.attachMedia(video);
        } catch { failed(); }
      }
    }
  }
}
