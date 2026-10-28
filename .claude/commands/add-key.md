---
description: Save an API key into .env and test it
argument-hint: <provider> <key>
---

The owner is giving you an API key: `$ARGUMENTS`

1. Work out the provider (groq, gemini, cerebras, openrouter, mistral, or a new one) and its `key_env` name from `providers.yaml`. If it's a new provider, add a block to `providers.yaml` using its documented OpenAI-compatible base URL.
2. Create `.env` from `.env.example` if it doesn't exist, then set that variable (replace any old value).
3. Run `uv run python ping.py` and report whether that provider now passes.
4. Never echo the full key back; show it masked (first 4 characters).
