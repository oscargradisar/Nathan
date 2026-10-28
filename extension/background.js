/*
 * Nathan Helper background worker. Talks to the Nathan app on 127.0.0.1 only, keeps the
 * one-time session per site for 5 minutes (so sign-in redirects don't lose it), and closes
 * the tab once Nathan has the key.
 */
"use strict";

const TTL_MS = 5 * 60 * 1000;

function base(port) {
  const p = Number(port);
  if (!Number.isInteger(p) || p < 1024 || p > 65535) throw new Error("bad port");
  return `http://127.0.0.1:${p}`;
}

async function getSession(host) {
  const key = `session:${host}`;
  const data = (await chrome.storage.session.get(key))[key];
  if (!data || data.expires < Date.now()) return null;
  return data;
}

async function handle(msg, sender) {
  switch (msg.type) {
    case "register": {
      await chrome.storage.session.set({
        [`session:${msg.host}`]: {
          token: msg.token, port: msg.port, provider: msg.provider, expires: Date.now() + TTL_MS,
        },
      });
      return { ok: true };
    }
    case "session":
      return await getSession(msg.host);
    case "config": {
      const res = await fetch(`${base(msg.port)}/onboarding.json`);
      return { ok: res.ok, data: res.ok ? await res.json() : null };
    }
    case "key": {
      const res = await fetch(`${base(msg.port)}/keys`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ provider: msg.provider, key: msg.key, token: msg.token }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) return { ok: false, error: body.detail || `HTTP ${res.status}` };
      const host = sender.tab && sender.tab.url ? new URL(sender.tab.url).host : null;
      if (host) await chrome.storage.session.remove(`session:${host}`);
      if (body.close && sender.tab) setTimeout(() => chrome.tabs.remove(sender.tab.id), 1500);
      return { ok: true };
    }
    default:
      return { ok: false, error: "unknown message" };
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  handle(msg, sender).then(sendResponse, (e) => sendResponse({ ok: false, error: String(e) }));
  return true; // keep the channel open for the async reply
});
