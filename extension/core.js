/*
 * Nathan Helper — page logic with no Chrome APIs, so it can be unit-tested in jsdom.
 *
 * Everything page-specific (button texts, key pattern) comes from Nathan's
 * /onboarding.json, which is built from providers.yaml. A dashboard redesign is a
 * config fix, not an extension update.
 */
(function (root) {
  "use strict";

  const KEY_NAME = "Nathan";
  const EDGE = "A-Za-z0-9_\\-";

  /** "#nathan=<token>&port=<port>&provider=<name>" → {token, port, provider} or null */
  function parseFragment(hash) {
    if (!hash) return null;
    const params = new URLSearchParams(hash.replace(/^#/, ""));
    const token = params.get("nathan");
    const port = Number(params.get("port"));
    const provider = params.get("provider");
    if (!token || !provider || !Number.isInteger(port) || port < 1024 || port > 65535) return null;
    if (!/^[A-Za-z0-9_-]{16,64}$/.test(token) || !/^[a-z0-9_-]{1,32}$/.test(provider)) return null;
    return { token, port, provider };
  }

  /** Find a full key in text. Neighbouring key characters disqualify a match, so
   *  "xgsk_…" or a longer string that merely contains a key-shaped run is rejected. */
  function findKeyIn(text, pattern) {
    if (!text || !pattern) return null;
    const re = new RegExp(`(?<![${EDGE}])(${pattern})(?![${EDGE}])`, "g");
    const m = re.exec(String(text));
    return m ? m[1] : null;
  }

  function norm(s) {
    return String(s || "").replace(/\s+/g, " ").trim().toLowerCase();
  }

  function isUsable(el) {
    if (!el) return false;
    if (el.disabled || el.getAttribute("aria-disabled") === "true") return false;
    if (el.hidden || el.closest("[hidden]")) return false;
    const style = el.ownerDocument.defaultView?.getComputedStyle?.(el);
    if (style && (style.display === "none" || style.visibility === "hidden")) return false;
    return true;
  }

  /** The first usable button-like element whose text matches one of `texts`
   *  (exact match preferred, then "starts with"). */
  function findByText(scope, texts, selector = "button, [role=button], a, input[type=submit]") {
    const wanted = (texts || []).map(norm).filter(Boolean);
    if (!wanted.length) return null;
    const els = Array.from(scope.querySelectorAll(selector)).filter(isUsable);
    const label = (el) => norm(el.textContent || el.value || el.getAttribute("aria-label"));
    for (const w of wanted) {
      const exact = els.find((el) => label(el) === w);
      if (exact) return exact;
    }
    for (const w of wanted) {
      const loose = els.find((el) => label(el).startsWith(w));
      if (loose) return loose;
    }
    return null;
  }

  function labelText(input) {
    const doc = input.ownerDocument;
    const parts = [input.placeholder, input.getAttribute("aria-label"), input.name, input.id];
    if (input.id) {
      const lab = Array.from(doc.querySelectorAll("label[for]")).find((l) => l.htmlFor === input.id);
      if (lab) parts.push(lab.textContent);
    }
    const wrap = input.closest("label");
    if (wrap) parts.push(wrap.textContent);
    const labelledby = input.getAttribute("aria-labelledby");
    if (labelledby) {
      for (const id of labelledby.split(/\s+/)) parts.push(doc.getElementById(id)?.textContent);
    }
    return norm(parts.filter(Boolean).join(" "));
  }

  /** The text input for the key's name: matched by label hints, else the only text
   *  input inside an open dialog. */
  function findNameField(scope, hints) {
    const inputs = Array.from(
      scope.querySelectorAll("input:not([type]), input[type=text]")
    ).filter((el) => isUsable(el) && !el.readOnly);
    for (const h of (hints || []).map(norm)) {
      const hit = inputs.find((el) => labelText(el).includes(h));
      if (hit) return hit;
    }
    const dialog = scope.querySelector("[role=dialog], dialog[open], [aria-modal=true]");
    if (dialog) {
      const inDialog = inputs.filter((el) => dialog.contains(el));
      if (inDialog.length === 1) return inDialog[0];
    }
    return null;
  }

  /** Set an input's value so React/Vue/Angular notice (they watch the native setter). */
  function setInputValue(input, value) {
    const proto = Object.getPrototypeOf(input);
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (setter) setter.call(input, value);
    else input.value = value;
    input.dispatchEvent(new input.ownerDocument.defaultView.Event("input", { bubbles: true }));
    input.dispatchEvent(new input.ownerDocument.defaultView.Event("change", { bubbles: true }));
  }

  /** Every key visible on the page right now: input values plus text. */
  function scanForKeys(doc, pattern) {
    const found = new Set();
    for (const el of doc.querySelectorAll("input, textarea")) {
      const k = findKeyIn(el.value, pattern);
      if (k) found.add(k);
    }
    const walker = doc.createTreeWalker(doc.body || doc, 4 /* NodeFilter.SHOW_TEXT */);
    let node;
    while ((node = walker.nextNode())) {
      const k = findKeyIn(node.nodeValue, pattern);
      if (k) found.add(k);
    }
    for (const el of doc.querySelectorAll("[data-value], [value]")) {
      const k = findKeyIn(el.getAttribute("data-value") || el.getAttribute("value"), pattern);
      if (k) found.add(k);
    }
    return found;
  }

  function highlight(doc, el, message) {
    const hint = doc.createElement("div");
    hint.setAttribute("data-nathan-hint", "");
    hint.textContent = "Nathan: " + message;
    hint.style.cssText =
      "position:fixed;z-index:2147483647;right:16px;bottom:16px;max-width:360px;" +
      "padding:12px 14px;border-radius:10px;background:#1f2937;color:#fff;" +
      "font:14px/1.4 system-ui,sans-serif;box-shadow:0 6px 24px rgba(0,0,0,.3)";
    (doc.body || doc.documentElement).appendChild(hint);
    if (el) {
      el.style.outline = "3px solid #f59e0b";
      el.style.outlineOffset = "2px";
      el.scrollIntoView?.({ block: "center" });
    }
    return hint;
  }

  function sleep(win, ms) {
    return new Promise((r) => win.setTimeout(r, ms));
  }

  async function waitFor(win, fn, timeoutMs, pollMs) {
    const end = Date.now() + timeoutMs;
    for (;;) {
      const v = fn();
      if (v) return v;
      if (Date.now() > end) return null;
      await sleep(win, pollMs);
    }
  }

  /**
   * Drive the key page: click Create → fill the name → confirm → capture the key.
   * cfg: {create_button, name_field, confirm_button, key_pattern}
   * opts: {onKey(key), onHint(msg), timeoutMs, pollMs}
   * Returns {stop()}.
   */
  function automate(doc, cfg, opts) {
    const win = doc.defaultView;
    const pollMs = opts.pollMs ?? 250;
    const timeoutMs = opts.timeoutMs ?? 8000;
    const onHint = opts.onHint || (() => {});
    const ignore = scanForKeys(doc, cfg.key_pattern); // keys already on screen aren't new
    let done = false;
    let observer = null;

    const check = () => {
      if (done) return;
      for (const k of scanForKeys(doc, cfg.key_pattern)) {
        if (!ignore.has(k)) {
          done = true;
          observer?.disconnect();
          opts.onKey(k);
          return;
        }
      }
    };

    observer = new win.MutationObserver(check);
    observer.observe(doc.documentElement, {
      subtree: true, childList: true, characterData: true, attributes: true,
      attributeFilter: ["value", "data-value"],
    });
    doc.addEventListener("copy", () => {
      const k = findKeyIn(win.getSelection?.().toString(), cfg.key_pattern);
      if (k && !done) {
        done = true;
        observer.disconnect();
        opts.onKey(k);
      }
    }, true);

    (async () => {
      const create = await waitFor(win, () => findByText(doc, cfg.create_button), timeoutMs, pollMs);
      if (done) return;
      if (!create) {
        onHint(`click "${(cfg.create_button || ["Create key"])[0]}" — I'll grab the key when it appears.`);
        return;
      }
      create.click();
      const dialogOf = () => doc.querySelector("[role=dialog], dialog[open], [aria-modal=true]");
      const findConfirm = () => {
        const d = dialogOf();
        const c = (d && findByText(d, cfg.confirm_button)) || findByText(doc, cfg.confirm_button);
        return c && c !== create ? c : null;
      };
      // Next screen: either a name field to fill, or straight to a confirm button.
      const next = await waitFor(
        win, () => findNameField(doc, cfg.name_field) || findConfirm(), 3000, pollMs);
      if (done) return;
      if (next && next.tagName === "INPUT") {
        setInputValue(next, KEY_NAME);
        await sleep(win, pollMs);
      }
      const confirm = await waitFor(win, findConfirm, 3000, pollMs);
      if (done) return;
      if (confirm) confirm.click();
      else onHint("finish creating the key — I'll capture it automatically.");
      check();
    })();

    return { stop: () => { done = true; observer.disconnect(); } };
  }

  const api = { parseFragment, findKeyIn, findByText, findNameField, setInputValue,
                scanForKeys, highlight, automate, KEY_NAME };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.NathanCore = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
