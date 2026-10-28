---
description: Full auto — build every remaining tier of Nathan without stopping
---

You are on autopilot. Lock in and build Nathan to completion.

Read `CLAUDE.md` and `docs/ROADMAP.md` now, then loop:

1. Find the first unfinished tier. Write a 3–5 line plan, then build it immediately — no approval needed.
2. Work in small steps. After each step: `uv run pytest`, `uv run ruff check . --fix`, fix anything red, tick the box in `docs/ROADMAP.md`, and `git commit` with a clear message. Push if a remote is set.
3. Run the tier's gate against the live providers. Passed → date it in the roadmap, commit, and go straight to the next tier. Failed → debug and fix until it passes.
4. Keep going until every tier in `docs/ROADMAP.md` is done.

Only stop early for things only the owner can do:
- a provider key is missing or invalid (say which one and give its signup link from `providers.yaml`),
- every provider's daily quota is used up (say when the earliest one resets),
- an action would cost money or break a hard rule in `CLAUDE.md`.

When you stop for any reason, write a short handoff at the top of `docs/ROADMAP.md` under `## Autopilot log`: date, what got built, what's blocked, and the exact next step. Then commit.

Never ask "should I continue?" — continue.

$ARGUMENTS
