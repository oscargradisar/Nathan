"""Ways of combining several models: fallback, race, vote, critique, manual lane."""

from nathan.patterns.fallback import fallback
from nathan.patterns.race import race
from nathan.patterns.vote import Candidate, VoteResult, vote

__all__ = ["Candidate", "VoteResult", "fallback", "race", "vote"]
