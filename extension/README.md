# Nathan Helper (Chrome / Edge / Brave extension)

Lets Nathan's **Connect** button fetch your free API keys for you. It only acts on a
provider's key page after you pressed Connect in Nathan (the tab URL carries a one-time
token), and it only talks to the Nathan app on `127.0.0.1`.

## Install (developer mode, until it's in the Chrome Web Store)

1. Open `chrome://extensions` (or `edge://extensions`, `brave://extensions`).
2. Turn on **Developer mode** (top right).
3. Click **Load unpacked** and pick this `extension/` folder.

Then run `uv run nathan connect all` (or use the Connect screen in the web UI).

## What it does on a key page

1. Clicks the "Create key" button (text comes from `providers.yaml` → `onboarding:`).
2. Types the key name **Nathan** and confirms.
3. Watches the page for text matching the provider's key pattern, sends it to Nathan,
   and closes the tab when Nathan confirms it saved the key in your OS keychain.

If the page was redesigned and a button isn't found, it shows a hint in the corner and
still captures the key once you create it yourself.

## Permissions

- `host_permissions`: the four key-page sites + `http://127.0.0.1/*`. No `<all_urls>`,
  no `cookies`, no `history`, no `tabs`.
- `storage`: remembers the one-time token for 5 minutes, so signing in on the provider
  site (which redirects away and back) doesn't lose it.

## Tests

```bash
cd extension && npm install && npm test   # jsdom, no browser, no network
```

The page fixtures in `test/fixtures/` are synthetic stand-ins that mimic each provider's
flow (button → name dialog → key shown). When a provider changes its page, update the
button text in `providers.yaml`, regenerate `test/onboarding.json` with
`uv run pytest tests/test_extension.py`, and adjust the fixture if the flow changed.
