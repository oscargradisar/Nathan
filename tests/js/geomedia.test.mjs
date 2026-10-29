import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { test } from "node:test";
import { viewportBox, inBox, inFrustum } from "../../nathan/web/static/js/geomedia-spatial.js";
import { frameUrl } from "../../nathan/web/static/js/media-inspector.js";

test("viewport bounds handle the dateline, poles and street zoom", () => {
  const box = viewportBox({ lat: 0, lng: 179.9, altitude: .01 });
  assert.ok(box[0] > box[2]);
  assert.ok(inBox([-179.9, 0], box));
  assert.ok(!inBox([0, 0], box));
  assert.deepEqual(viewportBox({ lat: 89, lng: 30, altitude: 2 }).filter((_, i) => i % 2 === 0), [-180, 180]);
  const street = viewportBox({ lat: 45, lng: 0, altitude: .00001 });
  assert.ok(street[2] - street[0] < 1);
});

test("frustum rejects far hemisphere and offscreen points", () => {
  const q = { camera: [0, 0, 300], radius: 100,
    matrix: [.01, 0, 0, 0, 0, .01, 0, 0, 0, 0, .001, 0, 0, 0, 0, 1] };
  assert.ok(inFrustum([0, 0], q));
  assert.ok(!inFrustum([180, 0], q));
  assert.ok(!inFrustum([0, 89], q));
  q.matrix[0] = .1;
  assert.ok(!inFrustum([30, 0], q));
});

test("image cache busting preserves queries and replaces timestamps", () => {
  assert.equal(frameUrl("https://example.test/frame?token=abc&_geomedia=1#x", 2), "https://example.test/frame?token=abc&_geomedia=2#x");
});

test("worker clusters 50,000 nodes, filters, expands and looks up IDs", async () => {
  const nodes = Array.from({ length: 50000 }, (_, i) => ({ id: String(i), title: `Node ${i}`,
    category: i % 2 ? "city" : "nature", coordinates: [(i * 137.5) % 360 - 180, (i * .37) % 160 - 80] }));
  const messages = [], context = vm.createContext({ console, inFrustum,
    fetch: async () => ({ ok: true, json: async () => ({ items: nodes }) }),
    self: { postMessage: (r) => messages.push(r) } });
  vm.runInContext(readFileSync(new URL("../../nathan/web/static/vendor/supercluster.min.js", import.meta.url), "utf8"), context);
  const worker = readFileSync(new URL("../../nathan/web/static/js/geomedia-worker.js", import.meta.url), "utf8").replace(/^import .*;\r?\n/gm, "");
  vm.runInContext(worker, context);
  await context.self.onmessage({ data: { type: "load" } });
  assert.equal(messages.at(-1).type, "ready");
  const query = { type: "query", request: 1, filters: {}, zoom: 0, bbox: [-180, -90, 180, 90],
    camera: [0, 0, 300], radius: 100, matrix: [.01, 0, 0, 0, 0, .01, 0, 0, 0, 0, .001, 0, 0, 0, 0, 1] };
  const start = performance.now();
  await context.self.onmessage({ data: query });
  assert.ok(messages.at(-1).features.some((f) => f.properties.cluster && f.expansionZoom > 0));
  assert.ok(messages.at(-1).features.length <= 600);
  console.log(`50k worker viewport query: ${(performance.now() - start).toFixed(1)} ms`);
  await context.self.onmessage({ data: { ...query, zoom: 21, filters: { category: "city" } } });
  assert.ok(messages.at(-1).features.length <= 600);
  await context.self.onmessage({ data: { ...query, filters: { category: "missing" } } });
  assert.equal(messages.at(-1).features.length, 0);
  await context.self.onmessage({ data: { type: "node", id: "123", request: 7 } });
  assert.equal(messages.at(-1).node.id, "123");
  assert.equal(messages.at(-1).request, 7);
  await context.self.onmessage({ data: { type: "replace", nodes: [{ ...nodes[0], id: "live-camera" }] } });
  await context.self.onmessage({ data: { type: "node", id: "live-camera", request: 8 } });
  assert.equal(messages.at(-1).node.id, "live-camera");
  await context.self.onmessage({ data: { type: "node", id: "123", request: 9 } });
  assert.equal(messages.at(-1).node, undefined);
});
