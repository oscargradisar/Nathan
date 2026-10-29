// The console's Markdown renderer must never let model output inject HTML.
import assert from "node:assert/strict";
import { test } from "node:test";
import { md, plain } from "../../nathan/web/static/js/core.js";

test("escapes HTML from models", () => {
  const out = md('<img src=x onerror="alert(1)"> **bold**');
  assert.ok(!out.includes("<img"));
  assert.ok(out.includes("&lt;img") && out.includes("<strong>bold</strong>"));
});

test("headings, lists, code, tables, dividers", () => {
  const out = md("# Title\n- a\n- b\n\n```\nx < y\n```\n\n| h1 | h2 |\n|---|---|\n| 1 | 2 |\n\n---");
  assert.ok(out.includes("<h2>Title</h2>"));
  assert.ok(out.includes("<ul>") && out.includes("<li>a</li>"));
  assert.ok(out.includes("<pre><code>x &lt; y</code></pre>"));
  assert.ok(out.includes("<th>h1</th>") && out.includes("<td>2</td>") && !out.includes("---|"));
  assert.ok(out.includes("<hr>"));
});

test("only http(s) links survive", () => {
  assert.ok(md("[ok](https://example.com)").includes('href="https://example.com"'));
  assert.ok(!md("[bad](javascript:alert(1))").includes("href"));
});

test("plain-language errors", () => {
  assert.match(plain("QuotaExceeded: groq 429"), /free limit/);
  assert.match(plain("NoModelAvailable: x"), /Connect another account/);
});

test("common LaTeX becomes plain symbols", () => {
  const out = md(String.raw`\(1001 = 7 \times 11 \times 13\) and $$\mathbf{x} \le 2$$`);
  assert.ok(out.includes("1001 = 7 × 11 × 13") && out.includes("x ≤ 2") && !out.includes("\\"));
});
