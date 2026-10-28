"""Load providers.yaml and API keys from .env.

Everything about which models exist and what their limits are comes from here,
so the rest of Nathan never hard-codes a model name.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "providers.yaml"


def data_dir() -> Path:
    """Where Nathan keeps its SQLite files (quota, memory, cache). Git-ignored."""
    path = Path(os.getenv("NATHAN_DATA", ROOT / "data"))
    path.mkdir(parents=True, exist_ok=True)
    return path


class Limits(BaseModel):
    rpm: int | None = None  # requests per minute
    rpd: int | None = None  # requests per day
    tpd: int | None = None  # tokens per day


class ModelSpec(BaseModel):
    tags: list[str] = Field(default_factory=list)
    limits: Limits = Field(default_factory=Limits)


class Provider(BaseModel):
    name: str
    base_url: str
    key_env: str
    signup: str | None = None
    account_limits: Limits = Field(default_factory=Limits)
    models: dict[str, ModelSpec]
    # Set by tests or the desktop app to bypass .env lookup. Never written to disk.
    key_override: str | None = Field(default=None, repr=False, exclude=True)

    @property
    def api_key(self) -> str | None:
        if self.key_override:
            return self.key_override
        key = os.getenv(self.key_env, "").strip()
        return key or None

    def ref(self, model: str) -> str:
        return f"{self.name}/{model}"


def load_providers(path: Path = DEFAULT_CONFIG, env_file: Path | None = ROOT / ".env") -> dict[str, Provider]:
    """Return {provider_name: Provider}. Loads .env first so keys are available."""
    if env_file is not None and env_file.exists():
        load_dotenv(env_file, override=False)
    raw = yaml.safe_load(path.read_text()) or {}
    providers = raw.get("providers", {})
    return {name: Provider(name=name, **spec) for name, spec in providers.items()}


def mask(key: str | None) -> str:
    """Show only the first 4 characters of a secret."""
    if not key:
        return "—"
    return key[:4] + "…"
