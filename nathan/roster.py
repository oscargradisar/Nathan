"""Pick models by what they're good for, never by hard-coded name.

The roster reads tags from providers.yaml ("fast", "judge", "long_context"...) and
filters out anything without a key, out of quota, or known to be gone. Patterns ask the
roster for "4 writing models from different providers" instead of naming models.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from nathan.config import Provider
from nathan.quota import QuotaManager

# Optional: eval scores (Tier 6) → {ref: score 0..1}. Higher wins ties.
ScoreFn = Callable[[str], float | None]


class Roster:
    def __init__(self, providers: dict[str, Provider], quota: QuotaManager | None = None,
                 score: ScoreFn | None = None) -> None:
        self.providers = providers
        self.quota = quota
        self.score = score

    def usable(self, ref: str) -> bool:
        provider, _, model = ref.partition("/")
        p = self.providers.get(provider)
        if p is None or not p.api_key:
            return False
        return self.quota is None or self.quota.available(provider, model)

    def candidates(self, tags: Iterable[str] | None = None) -> list[str]:
        """Usable models that have at least one of `tags` (all models if tags is empty),
        best first: more matching tags, better eval score, more capacity left."""
        wanted = set(tags or [])
        scored: list[tuple[tuple, str]] = []
        order = 0
        for p in self.providers.values():
            if not p.api_key:
                continue
            for model, spec in p.models.items():
                order += 1
                ref = p.ref(model)
                matches = len(wanted & set(spec.tags)) if wanted else 1
                if wanted and matches == 0:
                    continue
                if self.quota is not None and not self.quota.available(p.name, model):
                    continue
                remaining = self.quota.remaining_today(p.name, model) if self.quota else None
                capacity = remaining if remaining is not None else 10**6
                # Very small daily budgets (e.g. OpenRouter's 50/day) are kept for last.
                score = (self.score(ref) if self.score else None) or 0.0
                scored.append(((-matches, -score, -min(capacity, 1000), order), ref))
        scored.sort()
        return [ref for _, ref in scored]

    def pick(self, tags: Iterable[str] | None = None, n: int = 1, *, distinct_providers: bool = True,
             exclude: Iterable[str] = ()) -> list[str]:
        """Up to n models; spread across providers first so one outage can't sink a vote."""
        excluded = set(exclude)
        pool = [r for r in self.candidates(tags) if r not in excluded]
        chosen: list[str] = []
        used_providers: set[str] = set()
        for ref in pool:
            prov = ref.split("/", 1)[0]
            if distinct_providers and prov in used_providers:
                continue
            chosen.append(ref)
            used_providers.add(prov)
            if len(chosen) == n:
                return chosen
        if distinct_providers:  # not enough providers: allow repeats
            for ref in pool:
                if ref not in chosen:
                    chosen.append(ref)
                    if len(chosen) == n:
                        break
        return chosen

    def pick_one(self, tags: Iterable[str] | None = None, exclude: Iterable[str] = ()) -> str | None:
        picked = self.pick(tags, 1, exclude=exclude)
        if picked:
            return picked[0]
        any_model = self.pick(None, 1, exclude=exclude)  # nothing with that tag: anything usable
        return any_model[0] if any_model else None
