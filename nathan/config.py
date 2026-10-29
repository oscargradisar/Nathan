"""Load providers.yaml and API keys from .env.

Everything about which models exist and what their limits are comes from here,
so the rest of Nathan never hard-codes a model name.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "providers.yaml"


def workspace_dir() -> Path:
    """The only folder Nathan's file tools may read or write (Tier 4)."""
    path = Path(os.getenv("NATHAN_WORKSPACE", ROOT / "workspace")).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def data_dir() -> Path:
    """Where Nathan keeps its SQLite files (quota, memory, cache). Git-ignored."""
    path = Path(os.getenv("NATHAN_DATA", ROOT / "data"))
    path.mkdir(parents=True, exist_ok=True)
    return path


class Limits(BaseModel):
    rpm: int | None = None  # requests per minute
    rpd: int | None = None  # requests per day
    tpd: int | None = None  # tokens per day


NO_KEY = "no-key-needed"  # placeholder for keyless providers; the client strips it


def _var(name: str) -> str | None:
    """A ${VAR} in a base_url: from the environment, else saved in the keychain by Connect."""
    from nathan.keys import get_key

    return os.getenv(name) or get_key(name, name)


class ModelSpec(BaseModel):
    tags: list[str] = Field(default_factory=list)
    limits: Limits = Field(default_factory=Limits)


class Onboarding(BaseModel):
    """How to get a key with one click (Tier 4.5). Page details live here, not in code."""

    method: str = "extension"  # "extension" | "oauth_pkce"
    key_page: str = ""
    create_button: list[str] = Field(default_factory=list)   # button texts to click
    name_field: list[str] = Field(default_factory=list)      # label/placeholder hints
    confirm_button: list[str] = Field(default_factory=list)  # texts of the final button
    key_pattern: str = ""                                    # regex a real key matches
    account_id_url: str = ""  # Cloudflare-style lookup that fills the base_url's ${VAR}
    check_url: str = ""  # where to test a key, if not {base_url}/models
    note: str = ""

    def matches(self, key: str) -> bool:
        return bool(self.key_pattern) and re.fullmatch(self.key_pattern, key.strip()) is not None


class Provider(BaseModel):
    name: str
    base_url: str
    key_env: str = ""  # empty: a free gateway that needs no account or key
    signup: str | None = None
    models_url: str = ""  # where to list live models, if not {base_url}/models
    account_limits: Limits = Field(default_factory=Limits)
    models: dict[str, ModelSpec]
    # Set by tests or the desktop app to bypass .env lookup. Never written to disk.
    key_override: str | None = Field(default=None, repr=False, exclude=True)

    onboarding: Onboarding | None = None

    @property
    def missing_env(self) -> list[str]:
        """Variables referenced as ${VAR} in base_url that aren't set (e.g. an account id)."""
        return [v for v in self.url_vars if not _var(v)]

    @property
    def url_vars(self) -> list[str]:
        return re.findall(r"\$\{(\w+)\}", self.base_url)

    @property
    def resolved_base_url(self) -> str:
        return self.resolve(self.base_url)

    def resolve(self, text: str) -> str:
        """Fill ${VAR}s (like the Cloudflare account id) in a URL."""
        return re.sub(r"\$\{(\w+)\}", lambda m: _var(m.group(1)) or "", text)

    @property
    def api_key(self) -> str | None:
        if self.key_override:
            return self.key_override
        if self.missing_env:
            return None  # can't call this provider until its ${VAR}s are set
        if not self.key_env:
            return NO_KEY
        from nathan.keys import get_key

        return get_key(self.name, self.key_env)

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
    from nathan.keys import mask as _mask

    return _mask(key)
