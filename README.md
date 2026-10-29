# Nathan

**6 answers.. 1 answer.** Nathan runs a small company of free AI models. You ask once; a manager splits the job, a team of models works on the parts at the same time, a writer joins it up and a reviewer tells you what you asked for versus what you got. Operating cost: $0.

## Get it

Download the installer for your computer from the [latest release](https://github.com/oscargradisar/Nathan/releases/latest) and follow [docs/INSTALL.md](docs/INSTALL.md) (5 steps, no terminal).

## What it does

- **One console.** Type what you need and press Enter. *Auto* picks how to handle it: one fast model for small things, the whole team for big ones. Or choose: Quick, Workflow, Council (5 models + a judge), Swarm (up to 12 at once), Critique (draft → review → rewrite) or Agent (files, Python, web pages, and SQL on your CSV/SQLite data in the workspace folder).
- **You see who's working on what.** A live card for every model on the job, with timers, hand-offs and scores, and a log with dry commentary.
- **It squeezes the free tiers.** If a worker is slow, a backup starts on the same part and the first to finish wins. Parts the reviewer scores below 5/10 are redone by someone better. Planners and reviewers race each other. Repeated questions come from a cache and cost nothing.
- **It manages itself.** Every model has an Elo rating (Iron to Champion) earned from review scores; better-rated models get picked first, and quick models handle the jobs where waiting is the cost. At every start Nathan asks each provider which models still exist and retires the ones that vanished. An optional nightly check pings every model.
- **Prayers to Claude.** When the team is unsure, it drafts a question for Claude. You approve it, paste it into claude.ai (there's no free Claude API), paste the reply back, and the answer is revised.
- **Private.** Keys live in your computer's keychain and only go to their provider. Sound effects and voice are synthesized in the browser, so nothing is sent anywhere.

Free providers: Groq, Google AI Studio (Gemini), Cerebras, OpenRouter, NVIDIA, SambaNova, Cloudflare Workers AI, plus two gateways that need no account (Kilo Code, LLM7). Connect at least two in the app; no credit card, ever.

## For developers

```bash
uv sync                       # install dependencies (Python 3.12+)
uv run pytest                 # tests: no network, no quota
uv run python -m nathan.desktop   # the desktop app
uv run nathan web             # the console in your browser
uv run nathan ask "..."       # from the terminal; --mode auto|single|race|vote|critique|workflow|agent
uv run nathan health          # retire vanished models, ping the rest
uv run python packaging/build.py  # build the app folder (installers: packaging/, .github/workflows/release.yml)
```

- Models, limits and tags live only in `providers.yaml`; code never names a model.
- How it fits together: `client.py` (one client for every provider) → `quota.py` + `roster.py` + `staff.py` (who can work, who's good, who's fast) → `workflow.py` / `patterns/` → `web/` (FastAPI + one-page console in `web/static/`).
- Build rules and history: `CLAUDE.md`, `docs/ROADMAP.md`. Push a `v*` tag to build and publish installers for macOS, Windows x64 and Windows ARM64.
