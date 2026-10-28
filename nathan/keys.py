"""API keys: the OS keychain first, `.env` as the developer fallback.

The keychain (macOS Keychain, Windows Credential Manager, Linux Secret Service) stores
secrets encrypted and tied to your login, which is safer than a plain text file. The
`keyring` library talks to whichever one your OS has. On machines without one (servers,
CI), Nathan falls back to `.env`.

Set NATHAN_KEYCHAIN_ONLY=1 (the desktop app does) to ignore `.env` entirely.
"""

from __future__ import annotations

import contextlib
import os
import re
from pathlib import Path

SERVICE = "nathan"
ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"

_cache: dict[str, str | None] = {}


def mask(key: str | None) -> str:
    """Show only the first 4 characters of a secret."""
    if not key:
        return "—"
    return key[:4] + "…"


def keychain_available() -> bool:
    try:
        import keyring
        from keyring.backends import fail

        return not isinstance(keyring.get_keyring(), fail.Keyring)
    except Exception:  # noqa: BLE001 - any keyring import/backend problem means "not available"
        return False


def keychain_only() -> bool:
    return os.getenv("NATHAN_KEYCHAIN_ONLY", "") == "1"


def _keychain_get(provider: str) -> str | None:
    if provider in _cache:
        return _cache[provider]
    value = None
    if keychain_available():
        import keyring

        try:
            value = keyring.get_password(SERVICE, provider)
        except Exception:  # noqa: BLE001 - locked keychain etc.: behave as "no key"
            value = None
    _cache[provider] = value
    return value


def get_key(provider: str, key_env: str) -> str | None:
    value = _keychain_get(provider)
    if value:
        return value
    if keychain_only():
        return None
    env = os.getenv(key_env, "").strip()
    return env or None


def where(provider: str, key_env: str) -> str:
    if _keychain_get(provider):
        return "keychain"
    if not keychain_only() and os.getenv(key_env, "").strip():
        return ".env"
    return "missing"


def write_env(var: str, value: str, path: Path = ENV_FILE) -> None:
    """Set VAR=value in a .env file, replacing an existing line or appending one."""
    lines = path.read_text().splitlines() if path.exists() else []
    pattern = re.compile(rf"^\s*{re.escape(var)}\s*=")
    out, done = [], False
    for line in lines:
        if pattern.match(line):
            out.append(f"{var}={value}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"{var}={value}")
    path.write_text("\n".join(out) + "\n")


def set_key(provider: str, key_env: str, key: str, env_path: Path = ENV_FILE) -> str:
    """Save a key. Returns where it went: "keychain" or ".env"."""
    key = key.strip()
    _cache.pop(provider, None)
    if keychain_available():
        import keyring

        try:
            keyring.set_password(SERVICE, provider, key)
            _cache[provider] = key
            os.environ.pop(key_env, None)  # the keychain copy wins from now on
            return "keychain"
        except Exception:
            if keychain_only():
                raise
    if keychain_only():
        raise RuntimeError("no OS keychain available and NATHAN_KEYCHAIN_ONLY=1")
    write_env(key_env, key, env_path)
    os.environ[key_env] = key
    return ".env"


def delete_key(provider: str, key_env: str, env_path: Path = ENV_FILE) -> None:
    _cache.pop(provider, None)
    if keychain_available():
        import keyring

        with contextlib.suppress(Exception):  # nothing stored is fine
            keyring.delete_password(SERVICE, provider)
    _cache[provider] = None
    if env_path.exists():
        write_env(key_env, "", env_path)
    os.environ.pop(key_env, None)


def clear_cache() -> None:
    _cache.clear()
