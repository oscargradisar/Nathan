---
description: Verify the cloud-built tiers fast, finish Tier 6, then ship Nathan as a double-click desktop app
---

You're on the owner's computer, where the keys and a real browser are. A cloud run built Tiers 1–5 offline. Read `CLAUDE.md` and the `## Autopilot log` at the top of `docs/ROADMAP.md`. Then run the three phases below in order, without stopping between them. Keep going unless something needs the owner (a missing key, money, or a hard-rule conflict).

## Phase 1: Check the work (target: 15 minutes, as little quota as possible)

1. Run `uv sync`, `uv run ruff check .` and `uv run pytest -q`. Then run `cd extension && npm install && npm test`. Fix anything that fails.
2. Run `uv run python ping.py`. If fewer than 4 providers pass, run `uv run nathan connect all` (or `/add-key`) and stop only if a key is truly missing. If ping lists models as missing, run `/refresh-models`.
3. Run each live gate exactly once with the commands in the Autopilot log (Tiers 1, 2, 3, 4, 5). Use short prompts to save quota.
   - Pass: write today's date beside the gate.
   - Fail: find the root cause, fix it, add a mocked test that reproduces it, then rerun only that gate.
4. Tier 4.5 gate: test the Helper extension on one or two real key pages. If a button text is wrong, fix it in `providers.yaml` (not in code) and regenerate `extension/test/onboarding.json` with `uv run pytest tests/test_extension.py`.
5. Commit and push: "Live gates verified".

## Phase 2: Tier 6, kept small

Build Tier 6 from `docs/ROADMAP.md`:
- `nathan/health.py` plus a `nathan health` command. For each provider with a key, call `/models` and send a tiny test chat to every configured model. Mark dead models gone in the quota DB (the roster already skips them). `--apply` updates `providers.yaml` and must keep its comments (use `ruamel.yaml`).
- Response cache (`nathan/cache.py`). `Client` already has a `cache` hook and calls `nathan.cache.make_key`. Use SQLite, key on model + messages + params, and add a TTL. Never cache tool calls.
- `evals/tasks.yaml`: 30 tasks with checkable answers, and `nathan eval` to score models. Feed the scores into `Roster(score=...)`.
- Schedule `nathan health --apply` nightly with the OS scheduler: Task Scheduler on Windows, launchd on macOS. Prefer doing this from inside the desktop app (Phase 3).
- Gate: block a model with a mocked 404, then live by naming a fake model in `providers.yaml`. Nathan must reroute with no code change.

## Phase 3: Tier 7. An app you just open: no terminal, nothing technical

The owner must never need a terminal, `.env`, or the word "uv" again. Build:
1. **`nathan/desktop.py`:** start the existing `nathan.web.app` server on a free 127.0.0.1 port in a background thread. Open it in a native window with `pywebview` (add it as a dependency). Closing the window quits everything.
2. **First run:** if no provider is connected, the window opens on `/connect`. Add a big friendly "Step 1: install the Helper" card. It opens the browser's extensions page, shows the folder to load (or the Chrome Web Store link once published), and has a "Skip, I'll paste keys" option. After two or more providers connect, it moves on to the control room.
3. **Keys only in the OS keychain:** set `NATHAN_KEYCHAIN_ONLY=1` in the desktop entry point. Data goes to the OS app-data folder, never the install folder: set `NATHAN_DATA` and `NATHAN_WORKSPACE` with `platformdirs`.
4. **In-app settings page:** connected accounts, a quota view, a "Run health check now" button, a "Check every night" toggle (it installs or removes the OS scheduled task), the workspace folder with an "Open folder" button, and the app version.
5. **Plain-language errors:** replace raw exceptions everywhere in the UI. For example: "Groq's free limit for today is used up. Nathan switched to Gemini." or "No accounts connected yet: press Connect."
6. **Packaging:** use PyInstaller (`--windowed`, one folder) with the `static/` folder and `extension/` bundled. Name it `Nathan`, with an icon.
   - Windows: a `.exe` installer (Inno Setup) that adds Start-menu and Desktop shortcuts.
   - macOS: a `.app` in a `.dmg`.
   - Add a GitHub Actions workflow that builds both on a tag.
   - Build the owner's own platform locally now and put a shortcut on their Desktop.
7. **Tests:** a smoke test that imports `desktop.py` and starts and stops the server headless, with no window. Everything else is already mocked.
8. **Gate:** double-click the installed app on this computer, connect, and run a vote task with no terminal. Write `docs/INSTALL.md` (5 steps, screenshots optional) for a friend.

Tick boxes, date gates, commit after each working step, and push. Finish by rewriting the `## Autopilot log` with: what works, how to open the app, and anything the owner must do by hand (for example, publishing the extension to the Chrome Web Store, which needs a one-time $5 developer fee — say so and offer "Load unpacked" as the free path).
