# Nathan — free multi-model AI engine

Nathan takes one task, splits it into parts, sends the parts to several **free-tier AI APIs in parallel**, and merges the best answers into one result. Operating cost must stay at **$0**.

The full blueprint (architecture, model roster, patterns, risks) lives in the project doc "Nathan — Free Multi-Model AI Engine". The build checklist lives in `docs/ROADMAP.md` — that file is the source of truth for progress.

## Hard rules (never break these)

1. **Official free API keys only.** Call providers through their documented OpenAI-compatible endpoints. No scraping chat websites (chatgpt.com, claude.ai, chat.deepseek.com), no browser bots, no shared/leaked keys, no creating extra accounts to dodge limits.
2. **No Ollama, no paid services, no heavy wrapper frameworks** (no LangChain, LlamaIndex, LiteLLM, CrewAI, AutoGen). We write a thin layer ourselves so the owner understands every piece. Small, well-known libraries are fine (`openai`, `httpx`, `pydantic`, `pyyaml`, `typer`, `rich`, `fastapi`).
3. **Never hard-code model names or limits in logic.** They live only in `providers.yaml`. Free tiers change every few months.
4. **Keys live in `.env`, and you manage it.** You may read and edit `.env` freely (add keys the owner gives you, fix names, check which are set). Just never commit it or push it anywhere, and mask keys when printing (first 4 chars + `…`). If a key ever lands in a commit, tell the owner to rotate it.
5. **Tests never call real APIs.** Mock providers in tests so test runs burn zero quota. Only `ping.py` and explicit manual runs touch live endpoints.
6. **Never suggest adding a payment card** to any provider as a fix. If something needs money, say so and propose a free alternative.

## Providers (free tiers, as of Oct 2026)

| Provider | Base URL | Role |
| --- | --- | --- |
| Groq | `https://api.groq.com/openai/v1` | Main fast workers; `llama-3.1-8b-instant` for cheap routing |
| Google AI Studio (Gemini) | `https://generativelanguage.googleapis.com/v1beta/openai/` | Long context (1M), planner, judge — low request limits |
| Cerebras | `https://api.cerebras.ai/v1` | Fast second worker pool |
| OpenRouter | `https://openrouter.ai/api/v1` | `:free` models only; 50 requests/day for the WHOLE account — use sparingly |
| Mistral | `https://api.mistral.ai/v1` | Extra worker |

There is no free OpenAI or Anthropic API. ChatGPT/Claude quality comes in via the **manual lane** (Tier 5): Nathan writes a prompt the owner pastes into the website, then the owner pastes the answer back.

## Key onboarding

The end product is an app where a user presses **Connect** and their key arrives automatically. The design is in `docs/KEY_ONBOARDING.md`: OpenRouter via official OAuth PKCE, the other providers via the Nathan Helper browser extension. Keys go in the OS keychain and never leave the device. Don't use Playwright or Selenium to drive provider sign-in pages, because Google blocks sign-in from automated browsers.

## Commands

```bash
uv sync                      # install dependencies
uv run python ping.py        # Tier 0 gate: test every provider key
uv run pytest                # run tests (no network)
```

## How we work

- **Be autonomous.** The owner wants this as automated as possible: run commands, install packages, write and run tests, fix failures, and commit without asking. Only stop to ask when a choice is costly to undo or needs something only the owner can do (creating an account, pasting a key).
- Work **one tier at a time** from `docs/ROADMAP.md`. Don't start a tier until the previous tier's gate passes; when it passes, carry straight on to the next tier.
- Build in small steps with tests, and commit after each working step with a clear message.
- After finishing an item, tick its box in `docs/ROADMAP.md`. When a gate passes, note the date next to it.
- The owner is learning: explain *why* briefly when introducing a new concept (async, rate limiting, etc.), and keep code readable over clever.
- Async everywhere for network calls (`asyncio` + `openai.AsyncOpenAI`).
- Handle 429 (rate limited / quota gone) separately from 5xx/timeouts (provider down). Respect `Retry-After` and `x-ratelimit-*` headers when present.
- Code style: type hints, small modules, `ruff`-clean. Python 3.12+.

## Layout (grows by tier)

```
providers.yaml        # models, limits, capability tags (single source of truth)
ping.py               # Tier 0 connectivity check
nathan/
  config.py           # loads providers.yaml + .env
  client.py           # Tier 1: one chat() for every provider
  quota.py            # Tier 2: rate/daily limit tracking (SQLite)
  patterns/           # Tier 2–3: route, race, vote, critique, mapreduce, manual
  router.py planner.py executor.py   # Tier 3
  memory.py tools/    # Tier 4
  web/                # Tier 5
evals/                # Tier 6: owner's real test tasks
tests/
workspace/            # sandbox folder for file tools (Tier 4)
```
