// Run: cd extension && npm test   (node:test + jsdom, no browser, no network)
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");
const core = require("../core.js");

// Same JSON the Nathan app serves at /onboarding.json; pytest keeps this file in sync.
const CONFIG = JSON.parse(fs.readFileSync(path.join(__dirname, "onboarding.json"), "utf8"));

// Fake keys are assembled at runtime so secret scanners don't flag the test files.
const FAKE = {
  groq: "gsk_" + "Ab3D".repeat(13),
  gemini: "AIza" + "SyX-q_8".repeat(5),
  cerebras: "csk-" + "k4m9x2p7q1".repeat(5),
};

function load(name, key) {
  const html = fs.readFileSync(path.join(__dirname, "fixtures", `${name}.html`), "utf8");
  const dom = new JSDOM(html, {
    runScripts: "dangerously", pretendToBeVisual: true,
    beforeParse(window) { window.FAKE_KEY = key; },
  });
  return dom.window;
}

function capture(win, cfg, extra = {}) {
  return new Promise((resolve, reject) => {
    const hints = [];
    const timer = setTimeout(() => reject(new Error("no key captured; hints: " + hints.join(" | "))), 3000);
    core.automate(win.document, cfg, {
      pollMs: 5, timeoutMs: 300,
      onHint: (h) => { hints.push(h); extra.onHint && extra.onHint(h); },
      onKey: (k) => { clearTimeout(timer); resolve({ key: k, hints }); },
    });
  });
}

test("parseFragment accepts a valid fragment and rejects junk", () => {
  const tok = "abcDEF123_-abcDEF123_-xyz";
  assert.deepEqual(core.parseFragment(`#nathan=${tok}&port=51234&provider=groq`),
                   { token: tok, port: 51234, provider: "groq" });
  assert.equal(core.parseFragment(""), null);
  assert.equal(core.parseFragment(`#nathan=${tok}&port=80&provider=groq`), null);
  assert.equal(core.parseFragment(`#nathan=short&port=51234&provider=groq`), null);
  assert.equal(core.parseFragment(`#nathan=${tok}&port=51234&provider=<script>`), null);
});

test("findKeyIn: real shapes match, lookalikes don't", () => {
  for (const [name, key] of Object.entries(FAKE)) {
    const pat = CONFIG[name].key_pattern;
    assert.equal(core.findKeyIn(`your key: ${key} (copy it)`, pat), key, name);
    assert.equal(core.findKeyIn(`x${key}`, pat), null, `${name} prefixed`);
    assert.equal(core.findKeyIn(`${key}${"Z9".repeat(10)}`, pat), null, `${name} suffixed`);
  }
  assert.equal(core.findKeyIn("gsk_****Xy12", CONFIG.groq.key_pattern), null);
  assert.equal(core.findKeyIn("AIza...abc", CONFIG.gemini.key_pattern), null);
});

test("findByText prefers exact, skips disabled", () => {
  const win = new JSDOM(`<button disabled>Create API key in existing project</button>
    <button id="b">Create API key in new project</button><a id="a">Create API key</a>`).window;
  assert.equal(core.findByText(win.document, ["Create API key"]).id, "a");
  assert.equal(core.findByText(win.document, ["create api key in"]).id, "b");
  assert.equal(core.findByText(win.document, ["Nope"]), null);
});

test("findNameField by label, placeholder, aria-label, or lone dialog input", () => {
  const doc = new JSDOM(`<label for="n">Display name</label><input id="n">
    <input placeholder="Key name" id="p"><input aria-label="Name" id="a">
    <div role="dialog"><input id="only"></div>`).window.document;
  assert.equal(core.findNameField(doc, ["Display name"]).id, "n");
  assert.equal(core.findNameField(doc, ["key name"]).id, "p");
  assert.equal(core.findNameField(doc, ["zzz"]).id, "only");
});

for (const name of ["groq", "gemini", "cerebras"]) {
  test(`automate captures a new ${name} key from the fixture page`, async () => {
    const win = load(name, FAKE[name]);
    const { key } = await capture(win, CONFIG[name]);
    assert.equal(key, FAKE[name]);
  });
}

test("redesigned page: shows a hint, still captures when the user clicks", async () => {
  const win = load("manual", FAKE.groq);
  const { key, hints } = await capture(win, CONFIG.groq, {
    onHint: () => win.document.getElementById("x").click(),  // the user follows the hint
  });
  assert.equal(key, FAKE.groq);
  assert.match(hints[0], /click "Create API Key"/);
});

test("highlight adds a visible hint box", () => {
  const doc = new JSDOM("<button>x</button>").window.document;
  const el = doc.querySelector("button");
  const hint = core.highlight(doc, el, "click here");
  assert.equal(hint.textContent, "Nathan: click here");
  assert.match(el.style.outline, /3px/);
});
