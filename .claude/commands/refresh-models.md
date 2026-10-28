---
description: Check which free models still exist and update providers.yaml
---

Free tiers change often. Refresh the model roster:

1. For each provider with a key, call its `/models` endpoint and compare against `providers.yaml`.
2. Search the web for the provider's current free-tier rate limits page and note any changed limits.
3. Remove models that are gone, update limits that changed, and propose (then add) any strong new free models with sensible tags.
4. Run `uv run python ping.py` to confirm, then commit with a message listing what changed.
