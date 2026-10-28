# Nathan build roadmap

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

- [ ] FastAPI web app: each model's output streams live in its own column (server-sent events)
- [ ] Quota dashboard: remaining requests per provider today
- [ ] Manual lane: copy a ready-made prompt for claude.ai / chatgpt.com, paste the answer back as a candidate
- [ ] "Connect your AI accounts" wizard screen using the Tier 4.5 flows
- [ ] More providers: Cloudflare Workers AI, NVIDIA, SambaNova
- [ ] **Gate:** run a vote task from the browser and watch five lanes fill at once

## Tier 6 — Self-maintenance

- [ ] Nightly health check: discover models via each provider's `/models`, test them, update `providers.yaml`
- [ ] Eval set: 30 real tasks with expected answers in `evals/`; weekly scoring feeds the router
- [ ] Response cache so repeated questions cost no quota
- [ ] **Gate:** a provider drops a model and Nathan reroutes with no code change

## Tier 7 — An app anyone can install

- [ ] Package as a desktop app (Tauri or pywebview) with the Tier 5 UI inside
- [ ] First-run screen is the "Connect your AI accounts" wizard; the Helper extension installs from inside it
- [ ] Keys only in the OS keychain; nothing sent to any Nathan server
- [ ] Installers for Windows and macOS
- [ ] **Gate:** a friend with no setup installs it and runs a vote task within 5 minutes
