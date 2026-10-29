import "../vendor/supercluster.min.js";
import { inFrustum } from "./geomedia-spatial.js";

let nodes = [], byId = new Map(), index, filterKey = "";
let generation = 0;
function replace(items, warning) {
  nodes = items; byId = new Map(nodes.map((n) => [n.id, n]));
  build({}); filterKey = "{}";
  self.postMessage({ type: "ready", warning });
}
function build(filters) {
  const selected = nodes.filter((n) => Object.entries(filters).every(([k, v]) => !v || (n[k] || "").toLowerCase() === v.toLowerCase()));
  index = new globalThis.Supercluster({ radius: 60, maxZoom: 20 }).load(selected.map((n) => ({
    type: "Feature", geometry: { type: "Point", coordinates: n.coordinates }, properties: { id: n.id, title: n.title },
  })));
}
self.onmessage = async ({ data: q }) => {
  try {
    if (q.type === "load") {
      const version = ++generation;
      const response = await fetch("/api/geomedia/catalogue");
      if (!response.ok) throw new Error("Media catalogue failed to load");
      const result = await response.json();
      if (version === generation) replace(result.items, result.warning);
    } else if (q.type === "replace") {
      generation++; replace(q.nodes, "OpenCCTV public cameras · approximate source locations");
    } else if (q.type === "node") {
      self.postMessage({ type: "node", node: byId.get(q.id), request: q.request });
    } else if (q.type === "query" && index) {
      const key = JSON.stringify(q.filters);
      if (filterKey !== key) { build(q.filters); filterKey = key; }
      let zoom = q.zoom, features;
      // Bound HUD elements even for unusually dense feeds; zooming still expands clusters.
      do {
        features = index.getClusters(q.bbox, zoom).filter((f) => inFrustum(f.geometry.coordinates, q));
      } while (features.length > 600 && zoom-- > 0);
      self.postMessage({ type: "query", request: q.request, features: features.map((f) => ({
        ...f, expansionZoom: f.properties.cluster ? index.getClusterExpansionZoom(f.properties.cluster_id) : undefined,
      })) });
    }
  } catch (error) { self.postMessage({ type: "error", message: error.message }); }
};
