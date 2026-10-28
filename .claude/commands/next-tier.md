---
description: Build the next unfinished tier of Nathan end to end, autonomously
---

Build the next tier of Nathan.

1. Read `CLAUDE.md` and `docs/ROADMAP.md`. Find the first tier with unchecked boxes.
2. Confirm the previous tier's gate passes (run it). If it doesn't, fix that first.
3. Write a short plan for this tier in chat (files to add, how each piece works, how you'll test it), then go straight into building — don't wait for approval unless a step needs something only the owner can do (a new account, a key).
4. Build in small steps. After each step: run `uv run pytest` and `uv run ruff check .`, fix anything red, tick the matching box in `docs/ROADMAP.md`, and commit.
5. Run the tier's gate for real against the live providers. If it passes, write today's date beside the gate, commit, and summarize what now works plus one command the owner can try.
6. If quota runs out mid-gate, say which provider and when it resets, and stop there.

$ARGUMENTS
