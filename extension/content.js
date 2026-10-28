/*
 * Nathan Helper content script. Does nothing unless Nathan opened this tab with a
 * one-time token (#nathan=…), or this tab is continuing such a session after sign-in.
 */
(async () => {
  "use strict";
  const core = globalThis.NathanCore;
  const send = (msg) => chrome.runtime.sendMessage(msg);
  const host = location.host;

  const frag = core.parseFragment(location.hash);
  if (frag) {
    await send({ type: "register", host, ...frag });
    // Remove the token from the address bar so it doesn't end up in history or screenshots.
    history.replaceState(null, "", location.pathname + location.search);
  }
  const session = await send({ type: "session", host });
  if (!session || !session.token) return;

  const config = await send({ type: "config", port: session.port });
  const cfg = config && config.ok ? config.data[session.provider] : null;
  if (!cfg) {
    core.highlight(document, null, "couldn't reach the Nathan app. Is it still open?");
    return;
  }

  let hint = null;
  const showHint = (text) => {
    hint?.remove();
    hint = core.highlight(document, null, text);
  };

  const onKey = async (key) => {
    showHint("key captured — sending it to Nathan…");
    const res = await send({ type: "key", port: session.port, provider: session.provider,
                             key, token: session.token });
    showHint(res && res.ok ? "connected! This tab will close." :
             `Nathan didn't accept the key: ${res && res.error ? res.error : "unknown error"}`);
  };

  const onKeyPage = () => location.href.startsWith(cfg.key_page.split("#")[0]);
  if (!onKeyPage()) {
    // Not signed in yet (or landed elsewhere): wait for the user to reach the key page.
    showHint(`sign in or sign up here. When you're in, I'll continue on the key page: ${cfg.key_page}`);
    const timer = setInterval(() => {
      if (onKeyPage()) {
        clearInterval(timer);
        hint?.remove();
        core.automate(document, cfg, { onKey, onHint: showHint });
      }
    }, 1000);
    return;
  }
  core.automate(document, cfg, { onKey, onHint: showHint });
})();
