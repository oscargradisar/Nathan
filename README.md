# Nathan

Many free AI models working in parallel, one answer out. Zero operating cost.

## Get started (about 20 minutes)

**1. Install the tools** (once per computer)

- Python 3.12+: https://www.python.org/downloads/
- git: https://git-scm.com/downloads
- uv: https://docs.astral.sh/uv/getting-started/installation/
- Claude Code: https://docs.claude.com/en/docs/claude-code

**2. Get free API keys** (no credit card — don't add one)

| Provider | Get a key |
| --- | --- |
| Groq | https://console.groq.com/keys |
| Google AI Studio (Gemini) | https://aistudio.google.com/apikey |
| Cerebras | https://cloud.cerebras.ai |
| OpenRouter | https://openrouter.ai/keys |
| Mistral | https://console.mistral.ai/api-keys |

**3. Open the project in Claude Code**

```bash
cd nathan
claude
```

Then just paste each key in, e.g. `/add-key groq gsk_...` — Claude puts it in `.env` and tests it.

**4. Full auto: let Claude build the whole thing**

```bash
./autopilot.sh              # watch it work; type anytime to steer
./autopilot.sh --headless   # unattended in the background → tail -f autopilot.log
```

Autopilot runs with all approvals skipped, builds tier after tier, tests and commits as it goes, and only stops when it needs a key from you or quota runs out. It leaves a handoff note under "Autopilot log" in `docs/ROADMAP.md`. Inside a normal session, `/autopilot` does the same.

**Or step through it yourself**

| Command | What it does |
| --- | --- |
| `/status` | Provider health + where you are on the roadmap |
| `/next-tier` | Claude builds the next tier end to end, runs its gate, commits |
| `/add-key <provider> <key>` | Save a key and test it |
| `/refresh-models` | Check which free models still exist and update the config |

Progress is tracked in `docs/ROADMAP.md`. Claude's standing instructions are in `CLAUDE.md`.

## Manual commands

```bash
uv sync                    # install dependencies
uv run python ping.py      # test every provider
uv run pytest              # tests (no network, no quota used)

uv run nathan connect all  # connect AI accounts (keys go to your OS keychain)
uv run nathan ask "..."    # auto-routed answer; --mode single|race|vote|critique|plan|agent
uv run nathan web          # control room in your browser (live lanes, quota, manual lane)
uv run nathan quota        # free-tier usage in the last 24h
uv run nathan remember "…" / nathan recall "…"   # long-term memory
```

## Pushing to GitHub

```bash
git init && git add . && git commit -m "Nathan Tier 0 scaffold"
gh repo create nathan --private --source . --push
```

`.env` is git-ignored, so your keys stay on your computer.
