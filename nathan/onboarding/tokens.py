"""One-time tokens: proof that a key submission belongs to a Connect click.

Anything on your computer can send HTTP requests to 127.0.0.1, so the local server only
accepts a key together with a random token that Nathan created for that provider in the
last 5 minutes, and each token works once.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

TTL_S = 300.0


@dataclass
class _Entry:
    provider: str
    expires: float


class TokenStore:
    def __init__(self, ttl: float = TTL_S, clock: Callable[[], float] = time.time) -> None:
        self.ttl = ttl
        self.clock = clock
        self._tokens: dict[str, _Entry] = {}

    def issue(self, provider: str) -> str:
        token = secrets.token_urlsafe(24)
        self._tokens[token] = _Entry(provider, self.clock() + self.ttl)
        return token

    def valid(self, token: str | None, provider: str) -> bool:
        if not token:
            return False
        entry = self._tokens.get(token)
        if entry is None or entry.provider != provider:
            return False
        if entry.expires < self.clock():
            self._tokens.pop(token, None)
            return False
        return True

    def consume(self, token: str) -> None:
        self._tokens.pop(token, None)

    def active_provider(self) -> str | None:
        """The provider with the newest live token (the 'active step')."""
        now = self.clock()
        live = [e for e in self._tokens.values() if e.expires >= now]
        return max(live, key=lambda e: e.expires).provider if live else None
