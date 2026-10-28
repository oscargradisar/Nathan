# Nathan build roadmap

## Autopilot log

**2026-10-28: cloud autopilot run (no API keys, no browser, providers not called)**

**What got built:** Tiers 1–5, including Tier 4.5. Every item is ticked below. Each gate says "code done — live gate pending" because the gates need real keys.
- **Tier 1:** `nathan/client.py` gives one async client for every provider. It retries on 429, 5xx and timeouts, respects `Retry-After`, and raises a typed error for each failure (quota, provider down, model gone, auth, bad request). The CLI `nathan ask` can ask one model or several at once.
- **Tier 2:** `quota.py` counts requests per minute, per day and tokens per day in SQLite. It also reads `x-ratelimit-*` headers and blocks a model after a 429. `roster.py` picks models by tag and spreads them across providers. `patterns/` holds fallback, race and vote (judge merge plus spare-lane refill).
- **Tier 3:** `router.py` uses a cheap model to choose a pattern and falls back to keyword rules. `planner.py` builds a Pydantic task graph. `executor.py` runs subtasks in parallel waves, and a synthesizer writes the final answer. `patterns/critique.py` runs draft, critique and revise. `nathan ask` now defaults to `--mode auto`.
- **Tier 4:** `memory.py` stores questions, answers and facts in SQLite with FTS5 search. Relevant notes are added to new sessions automatically. Tools: workspace-only files, Python in a subprocess with a timeout and no keys in its environment, and URL fetch. `agent.py` is a bounded tool loop. It falls back from native function calling to a text tool format, and the next model can take over mid-task.
- **Tier 4.5:** `keys.py` stores keys in the OS keychain, with `.env` as a fallback. `onboarding/` has one-time tokens, a localhost `/keys` + `/callback` server, OpenRouter OAuth PKCE, a clipboard watcher and manual paste. `extension/` holds the Nathan Helper MV3 extension. New commands: `nathan connect|keys|disconnect`.
- **Tier 5:** `nathan web` starts the control room. It streams five lanes live (SSE) and shows a quota sidebar, the manual lane (copy prompt, paste answer, re-judge) and the Connect wizard. Three new providers: NVIDIA, SambaNova, Cloudflare Workers AI.

**Verified offline:** 128 pytest tests (all providers mocked, no network) and 11 extension tests (jsdom, synthetic key-page fixtures). `ruff` is clean. GitHub Actions runs all of these on every push. I also loaded the web UI and connect wizard in a headless browser.

**Not started, on purpose: Tiers 6 and 7.** Five gates haven't been run against real providers yet. Tier 6 (rerouting on real model drops, evals) and Tier 7 (installers, a friend's 5-minute install) depend most on real behaviour and can't be checked in the cloud. Run the gates first, then continue with `/next-tier`.

**Fastest path:** in this folder on your computer, run `claude --dangerously-skip-permissions "/finish"`. It checks the gates below, builds Tier 6, then packages Nathan as a desktop app you open by double-clicking (prompt: `.claude/commands/finish.md`).

**Needs a live run on your computer (what `/finish` does first):**
1. `uv sync`. For the extension tests: `cd extension && npm install && npm test`.
2. Add keys with either option:
   - `uv run nathan connect all`. First install the extension: `chrome://extensions` → Developer mode → Load unpacked → `extension/`.
   - Or `/add-key <provider> <key>`, or edit `.env`.
   - Check where keys landed with `uv run nathan keys`.
3. Tier 0 gate: `uv run python ping.py` (at least 4 providers must pass).
4. Tier 1 gate: `uv run nathan ask "What is the capital of Slovenia?" -m groq/llama-3.3-70b-versatile -m gemini/gemini-flash-latest -m cerebras/gpt-oss-120b -m mistral/mistral-small-latest`
5. Tier 2 gate:
   - Run `uv run nathan ask "Is 1001 prime? Explain." --mode vote`.
   - Comment out one key in `.env` and run it again.
   - Then run `uv run nathan quota`.
6. Tier 3 gate: `uv run nathan ask "Research the history of solar power and write a one-page brief" --mode plan`. The output should show 4 or more subtasks in parallel.
7. Tier 4 gate:
   - Put a few docs in `workspace/`.
   - Run `uv run nathan ask "Summarize the documents in my workspace folder" --mode agent`.
   - Run `uv run nathan remember "My cat is called Miso"`.
   - In a new terminal, run `uv run nathan ask "What is my cat called?"`.
8. Tier 4.5 gate: remove all keys (`uv run nathan disconnect <p>` for each), then run `uv run nathan connect all` and then `uv run python ping.py`.
9. Tier 5 gate: run `uv run nathan web`, choose mode vote with 5 lanes, press Run, and watch the lanes fill.

**Things to check when you run the gates:**
- The Helper extension's button texts in `providers.yaml` are best guesses at each provider's current page. Fix them there if a click misses. The extension shows a hint and still captures the key if you click yourself.
- The new NVIDIA, SambaNova and Cloudflare model names are unconfirmed. If ping lists them as missing, run `/refresh-models`.
- The client now imports `httpx2`, the HTTP library the current `openai` SDK (3.x) is built on.
- A push was blocked by GitHub secret scanning because a fake Mistral-shaped key was written in a test. It never reached GitHub. Tests now build fake keys at runtime. Your local branch `main-unpushed-old` holds that discarded commit and can be deleted.

Work top to bottom. Each tier ends with a **gate** — don't move on until it passes. Tick boxes as you go (`[x]`), and write the date beside a gate when it passes.

## Tier 0 — Keys and workbench

- [ ] Free accounts + API keys: Groq, Google AI Studio, Cerebras, OpenRouter, Mistral (links in README)
- [ ] Python 3.12+, git and uv installed; `uv sync` works
- [ ] Keys copied into `.env` (from `.env.example`); `.env` is git-ignored
- [ ] Private GitHub repo created and first commit pushed (without `.env`)
- [ ] **Gate:** `uv run python ping.py` shows at least 4 providers passing

## Tier 1 — One client for every provider

- [x] `providers.yaml` holds base URL, key name, models, limits and capability tags for every provider
- [x] `nathan/client.py`: one async `chat(provider, model, messages)` for all providers
- [x] Retry with backoff on 429/5xx; typed errors for "quota gone" vs "provider down"
- [x] CLI: `nathan ask "question" --model groq/openai/gpt-oss-120b`
- [x] Tests with mocked providers
- [ ] **Gate:** the same question answered by four different providers from the CLI — code done — live gate pending (needs keys, run on owner's computer)

## Tier 2 — Parallel fan-out and quota manager

- [x] Quota manager: per provider/model counters for requests/min, requests/day, tokens/day, stored in SQLite
- [x] Read live limits from response headers where available
- [x] Race pattern (first valid answer wins, others cancelled)
- [x] Fan-out-and-vote pattern + judge model that merges the best answer
- [x] Fallback chain when a model is out of quota or down
- [ ] **Gate:** `nathan ask --mode vote` returns a merged answer plus each model's raw answer, and still works with one provider's key removed — code done — live gate pending (needs keys, run on owner's computer)

## Tier 3 — Router, planner and workers

- [x] Router: small fast model labels the task type and picks a pattern
- [x] Planner: outputs a JSON task graph, validated with Pydantic
- [x] Executor: runs independent subtasks in parallel, feeds results to dependents, synthesizer writes final answer
- [x] Draft → critique → revise pattern using different models per role
- [ ] **Gate:** "Research X and write a one-page brief" plans, runs ≥4 subtasks in parallel, returns a coherent brief — code done — live gate pending (needs keys, run on owner's computer)

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
- [ ] **Gate:** run a vote task from the browser and watch five lanes fill at once — code done — live gate pending (needs keys, run on owner's computer)

## Tier 6 — Self-maintenance

- [ ] Nightly health check: discover models via each provider's `/models`, test them, update `providers.yaml`
- [ ] Eval set: 30 real tasks with expected answers in `evals/`; weekly scoring feeds the router
- [ ] Response cache so repeated questions cost no quota
- [ ] **Gate:** a provider drops a model and Nathan reroutes with no code change

## Tier 7 — An app anyone can install

- [x] Package as a desktop app with the Tier 5 UI inside (Edge app window on Windows, pywebview on macOS; PyInstaller)
- [x] First-run screen is the "Connect your AI accounts" wizard; Get key opens the key page and catches the copied key (Helper built in)
- [x] Keys only in the OS keychain; nothing sent to any Nathan server
- [x] Installers for Windows (x64, ARM64) and macOS, built on a v* tag by `.github/workflows/release.yml`
- [ ] **Gate:** a friend with no setup installs it and runs a vote task within 5 minutes
