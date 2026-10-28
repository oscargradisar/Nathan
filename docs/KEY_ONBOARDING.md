# Spec: one-click "Connect your AI accounts"

Goal: the user presses **Connect**, a browser tab opens on the provider's key page, and the key lands in Nathan automatically: no copying, no pasting, no settings files. Keys never leave the user's device.

## How it works

```
[Connect Groq] ──opens tab──▶ console.groq.com/keys  (user's real Chrome, already signed in)
                                   │
                    Nathan Helper extension (content script on that page)
                    1. clicks "Create API Key" (falls back to highlighting it)
                    2. fills the key name "Nathan"
                    3. reads the new key from the page the moment it appears
                                   │
                     POST http://127.0.0.1:<port>/keys  (one-time token)
                                   ▼
        Nathan app: test key (GET /models) → save to OS keychain → close tab → ✓
```

## Why an extension, not a robot browser

- **Google sign-in blocks automated browsers.** Playwright or Selenium-driven Chrome gets "This browser or app may not be secure", which breaks "Sign in with Google" on every provider. An extension runs in the user's real Chrome, so sign-in works normally and the user is often already logged in.
- **It's fast and invisible.** No second browser, no profile copying, no stored cookies.
- **It's narrow.** The extension only has permission on the provider key pages listed below and on `127.0.0.1`, and it only acts after the user pressed Connect in Nathan (the one-time token must match).

## Connection methods per provider

| Provider | Method | Key page | Key pattern |
| --- | --- | --- | --- |
| OpenRouter | **Official OAuth PKCE**: no extension needed (below) | `openrouter.ai/auth` | `sk-or-v1-…` |
| Groq | Extension auto-capture | `console.groq.com/keys` | `gsk_…` |
| Gemini | Extension auto-capture | `aistudio.google.com/apikey` | `AIza…` |
| Cerebras | Extension auto-capture | `cloud.cerebras.ai` (API Keys) | `csk-…` |
| Mistral | Extension auto-capture | `console.mistral.ai/api-keys` | 32 letters/digits (only while the Mistral step is active) |

All of this (URLs, button text, key patterns) lives in `providers.yaml` under each provider's `onboarding:` block, so a dashboard redesign is a config fix, not a code change. `/refresh-models` should also re-check these pages.

### OpenRouter: OAuth PKCE (zero clicks after login)

1. Nathan starts a temporary listener on `http://127.0.0.1:<random port>/callback`.
2. Opens `https://openrouter.ai/auth?callback_url=<listener>&code_challenge=<S256 challenge>&code_challenge_method=S256`.
3. User logs in and clicks **Authorize**; the browser returns to the listener with `?code=…`.
4. Nathan calls `POST https://openrouter.ai/api/v1/auth/keys` with `{code, code_verifier, code_challenge_method: "S256"}` and receives the key.
5. Test → keychain → close the listener.

### Extension auto-capture (Groq, Gemini, Cerebras, Mistral)

1. User presses **Connect <provider>**. Nathan makes a one-time token and opens `<key page>#nathan=<token>` in the default browser.
2. The extension's content script sees the token, then (from config) finds the "Create key" button by its text, clicks it, fills the name field with "Nathan", and confirms. If any element isn't found, it highlights where to click and shows a one-line hint instead.
3. A `MutationObserver` watches the page; the first text matching the provider's key pattern is captured. A `copy` event listener catches it too.
4. The script sends `{provider, key, token}` to `http://127.0.0.1:<port>/keys`. Nathan rejects anything without the matching token.
5. Nathan tests the key, saves it to the keychain, and tells the extension to close the tab. The wizard ticks green and moves to the next provider.
6. **Not signed up yet?** The page shows the provider's own sign-up; the extension waits on that tab and continues once the key page loads.

### Fallbacks (always available)

- **Clipboard watch:** if the extension isn't installed, Nathan opens the page and watches the clipboard for a matching key for 3 minutes (the user clicks Create, then Copy). It captures the key, then clears the clipboard.
- **Manual paste** box in Settings.

## Security rules

- Keys go into the OS keychain via `keyring` (macOS Keychain / Windows Credential Manager / Linux Secret Service). Developer mode may also use `.env`.
- Keys are never sent to any Nathan server; model calls go straight from the device to each provider.
- Nathan never sees or stores provider passwords or session cookies.
- The local endpoint listens on `127.0.0.1` only, requires the one-time token, and expires it after 5 minutes or one use.
- The extension requests only `host_permissions` for the five key pages + `http://127.0.0.1/*`, and no `cookies`, `history` or `<all_urls>` permission.

## The "Connect everything" wizard

- One screen: each provider with a status (not connected, waiting for you, connected, failed + reason) and how much free capacity it adds.
- Order: OpenRouter (OAuth) → Groq → Gemini → Cerebras → Mistral. **Connect all** runs them one after another.
- The user can stop after two; Nathan works with whatever is connected.
- **Disconnect** deletes the key from the keychain (and links to the provider page to revoke it).

## Build pieces

- `nathan/keys.py`: keychain get/set/delete, `.env` fallback, masking.
- `nathan/onboarding/server.py`: localhost listener (OAuth callback + `/keys` endpoint + token check).
- `nathan/onboarding/oauth_openrouter.py`: the PKCE flow.
- `nathan/onboarding/clipboard.py`: the fallback watcher (`pyperclip`).
- `extension/`: Manifest V3 Chrome extension (works in Chrome, Edge, Brave): `manifest.json`, `content.js` (driven by an `onboarding.json` the app serves), `background.js` (closes the tab when told).
- CLI first: `nathan connect [provider|all]`, then the wizard in the Tier 5 web UI and the Tier 7 desktop app.

## Tests (no real providers, no real keychain)

- PKCE challenge/verifier, state and token checks, code exchange (mocked HTTP).
- Key-pattern matching for every provider, including lookalikes that must be rejected.
- `/keys` endpoint: rejects a missing, wrong or expired token; accepts a valid one exactly once.
- Keychain round trip on an in-memory `keyring` backend.
- Extension content script: unit-test the matcher and button-finder against saved HTML fixtures of each key page (jsdom).
