# Nathan build roadmap

## Autopilot log

**2026-10-29: Nathan 1.0 (overnight run on the owner's Windows ARM64 laptop, with real keys)**

**How to open it:** double-click **Nathan** on the Desktop or in the Start menu. It opens on the console. First run (no accounts) opens on Accounts and moves to the console after two connect.

**What works (seen working live today):**
- **One console** replaces the four separate pages: a command bar with modes (Auto, Quick, Workflow, Council, Swarm, Critique, Agent), a core ring that tracks the job, a live card per model, the answer (Markdown, tables, LaTeX symbols), the asked-vs-got review, prayers to Claude, a commentary log, and Staff, Accounts, History and Systems views. Sound effects (Web Audio, no files), optional voice, mic input, keyboard shortcuts (`/`, `Alt+1…5`).
- **Every mode streams progress.** Critique had worked all along, just silently; it now shows drafter → critic → reviser.
- **Speed.** Auto had been routing through a 35-second "thinking" model; the router and Quick now use the fastest measured models (Quick question: about 3s end to end). Planners and reviewers race in pairs (planning went from 39s to 3s). A slow worker gets a backup after 20s, and the first to finish wins. The writer is the quickest of the ten best writers. A Lisbon weekend workflow took 56s, down from more than 2.5 minutes without finishing.
- **Self-managing.** Startup roll call (free) retires models providers stopped offering and reinstates returning ones. Systems check pings every model. Timed-out models sit out 5 minutes. Elo ranks every model and decides who gets picked. Parts scored below 5/10 are redone by someone else.
- **More staff:** 34 models across 9 providers, including 2 keyless gateways (Kilo Code, LLM7 open models). Dead models removed (Groq llama-3.x, Cerebras glm-4.7, three NVIDIA 404s, two Cloudflare 403s).
- **Database tool:** the agent can run SQL on `.csv`, `.db` and `.sqlite` files in the workspace folder.
- **Response cache** (24h), `nathan health`, nightly check toggle, Open workspace folder button.
- **Gates passed live today:** Tier 0, Tier 1, Tier 2, Tier 5 and Tier 6 (dated below).
- 156 Python tests + 5 console JS tests + 11 extension tests, all offline.

**For the owner to do by hand:**
- **Nightly check:** Systems → *Check every night* is **off**. Turning it on adds a Task Scheduler task on this computer that runs at 03:30, so that's your click.
- Mistral was removed: its free plan asks for a card.
- **Rotate the Cloudflare token** that was pasted into chat: delete it at dash.cloudflare.com → API Tokens, make a new one with the Workers AI template, paste it in Accounts.
- **Helper extension in the Chrome Web Store** (optional): publishing needs a one-time $5 developer fee. The free path works today: the app catches copied keys itself, and `chrome://extensions` → Developer mode → Load unpacked → `extension/` still works.
- **Tier 7 gate:** have a friend follow `docs/INSTALL.md` and time it.
- Untested: the macOS and Windows x64 builds on real machines, a prayer round trip with the real claude.ai, Swarm with more than 6 models live, and the agent's database tool with a live model.

**Notes for whoever works on this next:**
- The Claude desktop app redirects writes under `AppData\Roaming`; uv's Python therefore lives in `C:\Users\Oscar\.uv\python` (`UV_PYTHON_INSTALL_DIR`).
- Heredocs through this shell drop backslashes; write scripts with a file tool.
- Free tiers change: `nathan health` (or Systems → Run full systems check) shows what's alive; edit only `providers.yaml`.

Work top to bottom. Each tier ends with a **gate** — don't move on until it passes. Tick boxes as you go (`[x]`), and write the date beside a gate when it passes.

## Tier 0 — Keys and workbench

- [x] Free accounts + API keys: Groq, Google AI Studio, Cerebras, OpenRouter (+ NVIDIA, SambaNova, Cloudflare) (Mistral dropped: asks for a card)
- [x] Python 3.12+, git and uv installed; `uv sync` works
- [x] Keys stored (OS keychain via the app; `.env` still works for development and is git-ignored)
- [x] GitHub repo created and pushed (without `.env`)
- [x] **Gate:** at least 4 providers passing (2026-10-29: 9 providers answered in the systems check)

## Tier 1 — One client for every provider

- [x] `providers.yaml` holds base URL, key name, models, limits and capability tags for every provider
- [x] `nathan/client.py`: one async `chat(provider, model, messages)` for all providers
- [x] Retry with backoff on 429/5xx; typed errors for "quota gone" vs "provider down"
- [x] CLI: `nathan ask "question" --model groq/openai/gpt-oss-120b`
- [x] Tests with mocked providers
- [x] **Gate:** the same question answered by four different providers (2026-10-29: systems check and Council)

## Tier 2 — Parallel fan-out and quota manager

- [x] Quota manager: per provider/model counters for requests/min, requests/day, tokens/day, stored in SQLite
- [x] Read live limits from response headers where available
- [x] Race pattern (first valid answer wins, others cancelled)
- [x] Fan-out-and-vote pattern + judge model that merges the best answer
- [x] Fallback chain when a model is out of quota or down
- [x] **Gate:** vote returns a merged answer plus each model's raw answer and survives a provider dropping out (2026-10-29: Council in the console; a voter hit its quota and was replaced live)

## Tier 3 — Router, planner and workers

- [x] Router: small fast model labels the task type and picks a pattern
- [x] Planner: outputs a JSON task graph, validated with Pydantic
- [x] Executor: runs independent subtasks in parallel, feeds results to dependents, synthesizer writes final answer
- [x] Draft → critique → revise pattern using different models per role
- [ ] **Gate:** "Research X and write a one-page brief" plans, runs ≥4 subtasks in parallel, returns a coherent brief — live workflows ran 3 parts in parallel and 5 in total with a coherent result; ≥4 at once not yet seen

## Tier 4 — Memory and tools

- [x] Conversation/result log in SQLite with FTS5 full-text search
- [x] Tools: file read/write inside `workspace/`, Python in a subprocess with timeout, URL fetch
- [x] Bounded tool-calling loop (step limit) that works across providers
- [ ] **Gate:** answers questions about a folder of documents and recalls a fact from a previous session — code done — live gate pending (needs keys, run on owner's computer)

## Tier 4.5 — One-click key connection

Full spec: `docs/KEY_ONBOARDING.md`.

- [x] `nathan/keys.py`: keys in the OS keychain (`keyring`), `.env` fallback, masking
- [x] Localhost onboarding server with one-time tokens
- [x] OpenRouter OAuth PKCE: `nathan connect openrouter` gets a key with no copy-paste
- [x] Nathan Helper Chrome extension: opens the key page, clicks Create, captures the key, sends it to Nathan
- [x] Per-provider `onboarding:` config in `providers.yaml` (key page, button text, key pattern)
- [x] Clipboard-watch and manual-paste fallbacks
- [x] `nathan connect all` walks through every provider
- [ ] **Gate:** starting with no keys, `nathan connect all` connects at least 4 providers with zero copy-paste, and `ping.py` passes — code done — live gate pending (needs a real browser + accounts, run on owner's computer)

## Tier 5 — Control room UI and manual lane

- [x] FastAPI web app: each model's output streams live in its own column (server-sent events)
- [x] Quota dashboard: remaining requests per provider today
- [x] Manual lane: copy a ready-made prompt for claude.ai / chatgpt.com, paste the answer back as a candidate
- [x] "Connect your AI accounts" wizard screen using the Tier 4.5 flows
- [x] More providers: Cloudflare Workers AI, NVIDIA, SambaNova
- [x] **Gate:** run a vote task from the browser and watch five lanes fill at once (2026-10-29)

## Tier 6 — Self-maintenance

- [x] Health check: startup roll call via each provider's model list retires/reinstates models (no yaml edits needed), systems check pings each model, optional nightly task, `nathan health`
- [ ] Eval set: 30 real tasks with expected answers in `evals/` — not built; live Elo from reviewer scores feeds model choice instead
- [x] Response cache so repeated questions cost no quota
- [x] **Gate:** a provider drops a model and Nathan reroutes with no code change (2026-10-29: a fake Groq model was first in line, the roll call retired it, the request went to a real model)

## Tier 7 — An app anyone can install

- [x] Package as a desktop app with the Tier 5 UI inside (Edge app window on Windows, pywebview on macOS; PyInstaller)
- [x] First-run screen is the "Connect your AI accounts" wizard; Get key opens the key page and catches the copied key (Helper built in)
- [x] Keys only in the OS keychain; nothing sent to any Nathan server
- [x] Installers for Windows (x64, ARM64) and macOS, built on a v* tag by `.github/workflows/release.yml`
- [ ] **Gate:** a friend with no setup installs it and runs a vote task within 5 minutes
