import { $, el, toast } from "./core.js";
import { viewportBox } from "./geomedia-spatial.js";
import { MediaInspectorModal } from "./media-inspector.js";

export class GeoMediaLayer {
  constructor(world, onCount = () => {}) {
    this.world = world; this.visible = false; this.filters = {}; this.request = 0; this.selection = 0;
    this.inspector = new MediaInspectorModal();
    this.onCount = onCount; this.count = 0; this.features = new Map();
    this.worker = new Worker(new URL("./geomedia-worker.js", import.meta.url), { type: "module" });
    this.worker.onmessage = ({ data: r }) => {
      if (r.type === "ready") {
        this.ready = true; this.loading = false;
        $("media-status").textContent = r.warning || "Media catalogue ready"; this.query();
        if (this.pendingId) this.select(this.pendingId, true);
      } else if (r.type === "query" && r.request === this.request && this.visible) {
        this.count = r.features.reduce((sum, f) => sum + (f.properties.point_count || 1), 0);
        this.onCount();
        // Preserve object identity so camera queries reuse existing DOM markers.
        this.features = new Map(r.features.map((f) => {
          const key = f.properties.cluster ? `cluster:${f.properties.cluster_id}` : `node:${f.properties.id}`;
          return [key, this.features.get(key) || f];
        }));
        this.world.htmlElementsData([...this.features.values()]);
      } else if (r.type === "node" && r.request === this.selection && this.visible) {
        if (r.node) this.open(r.node); else toast("Media node not found.");
      } else if (r.type === "error") { this.loading = false; $("media-status").textContent = r.message; }
    };
    this.worker.onerror = () => { $("media-status").textContent = "Media worker unavailable. Reload the application to retry."; this.ready = false; };
    // globe.gl's default minimum altitude is ~64 km; media needs street-level zoom.
    world.controls().minDistance = world.getGlobeRadius() * 1.000001;
    world.htmlLat((f) => f.geometry.coordinates[1]).htmlLng((f) => f.geometry.coordinates[0])
      .htmlAltitude(0).htmlElement((f) => {
        const cluster = f.properties.cluster;
        return el("button", { class: "media-marker" + (cluster ? " cluster" : ""),
          "aria-label": cluster ? `Expand ${f.properties.point_count} media nodes` : f.properties.title,
          onclick: () => cluster ? this.fly(f.geometry.coordinates, 2 ** (2 - f.expansionZoom)) : this.select(f.properties.id),
        }, cluster ? String(f.properties.point_count) : "▶");
      });
    // Throttle continuous auto-rotation and debounce the final camera position.
    world.controls().addEventListener("change", () => {
      clearTimeout(this.debounce);
      this.debounce = setTimeout(() => this.query(), 180);
      if (!this.timer) this.timer = setTimeout(() => { this.timer = null; this.query(); }, 250);
    });
    this.loading = true;
    this.worker.postMessage({ type: "load" });
  }
  setVisible(visible) {
    this.visible = visible;
    $("media-status").hidden = !visible;
    if (visible && !this.ready && !this.loading) { this.loading = true; this.worker.postMessage({ type: "load" }); }
    if (visible) this.query();
    else { this.request++; this.selection++; this.count = 0; this.onCount(); this.world.htmlElementsData([]); this.features.clear(); this.inspector.close(); }
  }
  filter(filters) { this.filters = filters; this.features.clear(); this.query(); }
  replace(nodes, id) {
    this.ready = false; this.features.clear(); this.filters = {}; this.pendingId = id;
    this.autoQueue = nodes.slice(0, 5).map((n) => n.id);
    this.worker.postMessage({ type: "replace", nodes });
  }
  query() {
    if (!this.visible || !this.ready || $("view-world").hidden) return;
    const pov = this.world.pointOfView(), camera = this.world.camera();
    camera.near = Math.max(0.000001, Math.min(0.1, pov.altitude * this.world.getGlobeRadius() / 10));
    camera.updateProjectionMatrix();
    camera.updateMatrixWorld();
    const matrix = camera.projectionMatrix.clone().multiply(camera.matrixWorldInverse).elements;
    this.worker.postMessage({ type: "query", request: ++this.request, filters: this.filters,
      bbox: viewportBox(pov), zoom: Math.max(0, Math.min(21, Math.floor(Math.log2(4 / Math.max(0.000001, pov.altitude))))),
      matrix: [...matrix], camera: camera.position.toArray(), radius: this.world.getGlobeRadius(),
    });
  }
  select(id, automatic = false) {
    if (!automatic) this.autoQueue = [];
    this.pendingId = id;
    if (this.ready) { this.pendingId = null; this.worker.postMessage({ type: "node", id, request: ++this.selection }); }
  }
  fly([lng, lat], altitude = 0.15) {
    this.world.controls().autoRotate = false;
    this.world.pointOfView({ lng, lat, altitude: Math.max(0.000001, altitude) }, 1200);
  }
  open(node) {
    this.activeNode = node; this.fly(node.coordinates);
    this.inspector.open(node, () => {
      const next = (this.autoQueue || []).filter((id) => id !== node.id);
      this.autoQueue = next;
      if (next.length) this.select(next[0], true);
    });
  }
}
