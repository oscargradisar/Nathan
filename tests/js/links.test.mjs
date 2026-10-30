// Source links come from scraped pages: only http(s) may become a clickable href.
import assert from "node:assert/strict";
import { test } from "node:test";
import { webUrl } from "../../nathan/web/static/js/core.js";

test("web links pass through", () => {
  assert.equal(webUrl("https://example.com/a?b=1"), "https://example.com/a?b=1");
  assert.equal(webUrl("HTTP://example.com"), "HTTP://example.com");
});

test("anything else is dropped", () => {
  for (const bad of ["javascript:alert(1)", " javascript:alert(1)", "data:text/html,hi", "//evil.example", "", null, undefined, 42]) {
    assert.equal(webUrl(bad), null, String(bad));
  }
});
