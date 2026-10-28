"""Typed errors so callers can react differently to each kind of failure.

Why separate types? A 429 ("you're over the limit") means "try a different model or
wait"; a 5xx or timeout means "this provider is down right now"; a 404 on a model
means "this model is gone, stop picking it". Patterns like fallback and vote use these
types to decide what to do next.
"""

from __future__ import annotations


class NathanError(Exception):
    """Base class for every error Nathan raises on purpose."""

    def __init__(self, message: str, *, provider: str = "", model: str = "") -> None:
        super().__init__(message)
        self.provider = provider
        self.model = model


class QuotaExceeded(NathanError):
    """Rate limited (HTTP 429) or our own quota tracker says the limit is used up."""

    def __init__(self, message: str, *, provider: str = "", model: str = "",
                 retry_after: float | None = None) -> None:
        super().__init__(message, provider=provider, model=model)
        self.retry_after = retry_after  # seconds until it is worth trying again, if known


class ProviderDown(NathanError):
    """5xx, timeout or connection failure, after retries."""


class ModelGone(NathanError):
    """The provider says this model does not exist (HTTP 404 / model_not_found)."""


class AuthError(NathanError):
    """Missing or rejected API key (HTTP 401/403, or no key configured)."""


class BadRequest(NathanError):
    """The provider rejected the request itself (HTTP 400/422). Retrying won't help."""


class NoModelAvailable(NathanError):
    """Every candidate model was out of quota, down, or had no key."""


# Errors after which it makes sense to try another model
RECOVERABLE = (QuotaExceeded, ProviderDown, ModelGone, AuthError, BadRequest)
