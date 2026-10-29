// Shared helpers for the Nathan console. Plain ES modules, no build step.
export const $ = (id) => document.getElementById(id);

export function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids) if (kid != null && kid !== false) e.append(kid);
  return e;
}

// POSTs carry X-Nathan so other websites can't drive the local server (see web/app.py).
export async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST", headers: { "content-type": "application/json", "x-nathan": "1" },
    body: JSON.stringify(body),
  };
  const r = await fetch(path, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(plain(j.detail || `HTTP ${r.status}`));
  return j;
}

// Follow a run's server-sent events. Returns a function that stops listening.
export function follow(runId, since, onEvent) {
  const src = new EventSource(`/api/runs/${runId}/events?since=${since}`);
  src.onmessage = (m) => {
    const ev = JSON.parse(m.data);
    onEvent(ev);
    if (ev.type === "done") src.close();
  };
  src.onerror = () => { /* the browser retries by itself; "done" closes it */ };
  return () => src.close();
}

export const store = {
  get(k, d) { try { const v = localStorage.getItem("nathan." + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("nathan." + k, JSON.stringify(v)); } catch { /* private mode */ } },
};

export const short = (ref) => (ref || "").split("/").slice(1).join("/") || ref || "";
export const provider = (ref) => (ref || "").split("/")[0];
export const clock = (d = new Date()) => d.toTimeString().slice(0, 8);
export const pick = (arr) => arr[Math.floor(Math.random() * arr.length)];

let toastTimer;
export function toast(text, ms = 2600) {
  const t = $("toast");
  t.textContent = text; t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, ms);
}

// Turn raw error text into something a person can act on.
export function plain(msg) {
  const m = String(msg);
  if (/QuotaExceeded|429/.test(m)) return "That provider's free limit is used up for now. Nathan will use someone else.";
  if (/NoModelAvailable|no model/i.test(m)) return "No connected model could take this. Connect another account, or try again in a minute.";
  if (/AuthError|401|403/.test(m)) return "A provider rejected its key. Check it under Accounts.";
  if (/ProviderDown|timeout|timed out/i.test(m)) return "A provider is slow or down right now. Nathan moved on where it could.";
  return m.replace(/^internal error: /, "");
}

// ---- tiny, safe Markdown: escape everything first, then add a few tags back ----
const esc = (s) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
function inline(s) {
  return s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
}
export function md(src) {
  const lines = esc(String(src || "")).split("\n");
  const out = [];
  let list = null, code = null, para = [], table = [];
  const flush = () => { if (para.length) { out.push(`<p>${inline(para.join(" "))}</p>`); para = []; } };
  const close = () => { if (list) { out.push(`</${list}>`); list = null; } };
  const cells = (row) => row.trim().replace(/^\||\|$/g, "").split("|").map((c) => inline(c.trim()));
  const endTable = () => {
    if (!table.length) return;
    const rows = table.filter((r) => !/^\s*\|?\s*:?-{2,}/.test(r));  // drop the |---| divider
    const [head, ...body] = rows;
    out.push("<table><thead><tr>" + cells(head).map((c) => `<th>${c}</th>`).join("") + "</tr></thead><tbody>"
      + body.map((r) => "<tr>" + cells(r).map((c) => `<td>${c}</td>`).join("") + "</tr>").join("") + "</tbody></table>");
    table = [];
  };
  for (const line of lines) {
    if (code !== null) {
      if (/^```/.test(line)) { out.push(`<pre><code>${code.join("\n")}</code></pre>`); code = null; } else code.push(line);
      continue;
    }
    if (/^\s*\|.*\|\s*$/.test(line)) { flush(); close(); table.push(line); continue; }
    endTable();
    if (/^```/.test(line)) { flush(); close(); code = []; continue; }
    const h = line.match(/^(#{1,4})\s+(.*)/);
    const ul = line.match(/^\s*[-*•]\s+(.*)/);
    const ol = line.match(/^\s*\d+[.)]\s+(.*)/);
    if (h) { flush(); close(); const n = Math.min(h[1].length + 1, 4); out.push(`<h${n}>${inline(h[2])}</h${n}>`); }
    else if (ul || ol) {
      flush();
      const kind = ul ? "ul" : "ol";
      if (list !== kind) { close(); out.push(`<${kind}>`); list = kind; }
      out.push(`<li>${inline((ul || ol)[1])}</li>`);
    } else if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { flush(); close(); out.push("<hr>"); }
    else if (/^&gt;\s?/.test(line)) { flush(); close(); out.push(`<blockquote>${inline(line.replace(/^&gt;\s?/, ""))}</blockquote>`); }
    else if (!line.trim()) { flush(); close(); }
    else { close(); para.push(line); }
  }
  if (code !== null) out.push(`<pre><code>${code.join("\n")}</code></pre>`);
  endTable(); flush(); close();
  return out.join("\n");
}
